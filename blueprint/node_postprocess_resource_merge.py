import bpy
import os
import glob
import hashlib
import re
import tempfile

from .node_postprocess_base import SSMTNode_PostProcess_Base


class SSMTNode_PostProcess_ResourceMerge(SSMTNode_PostProcess_Base):
    bl_idname = 'SSMTNode_PostProcess_ResourceMerge'
    bl_label = '资源合并'
    bl_description = '通过计算DDS贴图内容的MD5哈希值，自动合并内容相同的资源引用并删除重复的DDS贴图'

    def draw_buttons(self, context, layout):
        layout.label(text="计算DDS贴图文件MD5哈希值", icon='FILE_CACHE')
        layout.label(text="合并内容相同的DDS资源引用")
        layout.label(text="自动删除重复的DDS贴图文件")
        layout.separator()
        layout.label(text="执行前会自动备份ini文件", icon='BACK')

    def compute_file_hash(self, file_path, block_size=65536):
        if not os.path.exists(file_path):
            return None
        hasher = hashlib.md5()
        try:
            with open(file_path, 'rb') as f:
                while True:
                    data = f.read(block_size)
                    if not data:
                        break
                    hasher.update(data)
            return hasher.hexdigest()
        except (OSError, IOError):
            return None

    @staticmethod
    def _is_resource_section(section_name: str) -> bool:
        normalized = str(section_name or "").strip()
        if not (normalized.startswith("[") and normalized.endswith("]")):
            return False
        normalized = normalized[1:-1]
        return normalized.lower().startswith("resource")

    @staticmethod
    def _is_dds_filename(filename: str) -> bool:
        return os.path.splitext(str(filename or "").strip())[1].lower() == ".dds"

    @staticmethod
    def _resolve_mod_local_path(mod_export_path: str, filename: str) -> str | None:
        """Resolve an INI filename only when its real path stays inside the mod root."""
        root = os.path.realpath(os.path.abspath(mod_export_path))
        relative = str(filename or "").replace("\\", os.sep).replace("/", os.sep)
        candidate = os.path.realpath(os.path.abspath(os.path.join(root, relative)))
        root_compare = os.path.normcase(root)
        candidate_compare = os.path.normcase(candidate)
        try:
            if os.path.commonpath((root_compare, candidate_compare)) != root_compare:
                return None
        except ValueError:
            # Different Windows drives have no common path.
            return None
        return candidate

    @classmethod
    def _resource_filename_entries(cls, content: str):
        """Return ordered resource filename records without rebuilding the INI.

        Duplicate section names and preamble text deliberately remain represented by
        their original line positions. Animation-driver and auto-appended blocks are
        protected from this postprocessor.
        """
        lines = content.splitlines(keepends=True)
        filename_pattern = re.compile(
            r"^(?P<prefix>[ \t]*filename[ \t]*=[ \t]*)"
            r"(?P<value>.*?)(?P<trailing>[ \t]*)(?P<newline>\r?\n)?$",
            re.IGNORECASE,
        )
        driver_start = getattr(
            cls, "ANIM_DRIVER_SECTION_MARKER_START", "; --- ANIMATION DRIVER SECTION ---"
        )
        driver_end = getattr(
            cls, "ANIM_DRIVER_SECTION_MARKER_END", "; --- END ANIMATION DRIVER SECTION ---"
        )
        tail_detector = getattr(cls, "is_known_auto_appended_marker", None)
        in_driver = False
        current_section = ""
        current_is_resource = False
        current_has_filename = False
        section_count = 0
        entries = []

        for line_index, line in enumerate(lines):
            if driver_start in line:
                in_driver = True
                current_section = ""
                current_is_resource = False
                continue
            if in_driver:
                if driver_end in line:
                    in_driver = False
                continue
            if callable(tail_detector) and tail_detector(line):
                break

            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                section_count += 1
                current_section = stripped
                current_is_resource = cls._is_resource_section(stripped)
                current_has_filename = False
                continue
            if not current_is_resource or current_has_filename:
                continue
            match = filename_pattern.match(line)
            if match is None:
                continue
            current_has_filename = True
            entries.append({
                "section": current_section,
                "line_index": line_index,
                "filename": match.group("value").strip(),
                "match": match,
            })

        return lines, entries, section_count

    @staticmethod
    def _atomic_write_text(path: str, content: str):
        folder = os.path.dirname(os.path.abspath(path))
        fd, temporary_path = tempfile.mkstemp(prefix=".resource-merge-", suffix=".tmp", dir=folder)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as file_obj:
                file_obj.write(content)
            os.replace(temporary_path, path)
        except Exception:
            try:
                os.remove(temporary_path)
            except OSError:
                pass
            raise

    def execute_postprocess(self, mod_export_path):
        print(f"[ResourceMerge] 开始执行，Mod导出路径: {mod_export_path}")
        print(f"[ResourceMerge] 路径是否存在: {os.path.exists(mod_export_path)}")

        ini_files = glob.glob(os.path.join(mod_export_path, "*.ini"))
        print(f"[ResourceMerge] 找到 {len(ini_files)} 个ini文件: {ini_files}")
        if not ini_files:
            print("[ResourceMerge] 在路径中未找到任何.ini文件，跳过")
            return

        for ini_file in ini_files:
            self.process_ini_file(ini_file, mod_export_path)

        print("[ResourceMerge] 资源引用合并完成！")

    def process_ini_file(self, ini_file, mod_export_path):
        print(f"[ResourceMerge] 正在处理ini文件: {ini_file}")
        self._create_cumulative_backup(ini_file, mod_export_path)

        with open(ini_file, 'r', encoding='utf-8', newline='') as f:
            content = f.read()

        lines, resource_entries, section_count = self._resource_filename_entries(content)
        print(
            f"[ResourceMerge] ini中共有 {section_count} 个section，"
            f"其中 {len(resource_entries)} 个Resource section含filename"
        )

        file_hash_to_first_ref = {}
        files_to_delete: dict[str, str] = {}

        for entry in resource_entries:
            section_name = entry["section"]
            filename = entry["filename"]
            if not self._is_dds_filename(filename):
                print(f"[ResourceMerge] 跳过 {section_name}: 非DDS贴图 {filename}")
                continue

            file_path = self._resolve_mod_local_path(mod_export_path, filename)
            if file_path is None:
                print(f"[ResourceMerge] 跳过 {section_name}: 路径越出Mod目录 {filename}")
                continue
            if not os.path.isfile(file_path):
                print(f"[ResourceMerge] 跳过 {section_name}: 文件不存在 {file_path}")
                continue

            file_hash = self.compute_file_hash(file_path)
            if not file_hash:
                print(f"[ResourceMerge] 跳过 {section_name}: 无法计算哈希")
                continue

            print(f"[ResourceMerge] {section_name} -> {filename} (MD5: {file_hash[:16]}...)")

            if file_hash in file_hash_to_first_ref:
                primary_ref = file_hash_to_first_ref[file_hash]
                primary_path = os.path.normcase(os.path.realpath(primary_ref['file_path']))
                duplicate_path = os.path.normcase(os.path.realpath(file_path))
                if duplicate_path != primary_path:
                    files_to_delete[file_path] = filename
                    print(f"[ResourceMerge]   重复! 与 {primary_ref['section']} 相同")
                else:
                    print(f"[ResourceMerge]   共享文件引用，与 {primary_ref['section']} 指向同一路径，保留文件")
            else:
                file_hash_to_first_ref[file_hash] = {
                    'section': section_name,
                    'filename': filename,
                    'file_path': file_path,
                }
            entry["file_hash"] = file_hash

        print(f"[ResourceMerge] 扫描完成: {len(file_hash_to_first_ref)} 个唯一资源, {len(files_to_delete)} 个重复文件待删除")

        modified = False
        for entry in resource_entries:
            file_hash = entry.get("file_hash")
            if not file_hash or file_hash not in file_hash_to_first_ref:
                continue
            original_filename = entry["filename"]
            primary_filename = file_hash_to_first_ref[file_hash]["filename"]
            if original_filename == primary_filename:
                continue
            match = entry["match"]
            lines[entry["line_index"]] = (
                match.group("prefix")
                + primary_filename
                + match.group("trailing")
                + (match.group("newline") or "")
            )
            modified = True
            print(f"[ResourceMerge] 引用替换: {original_filename} -> {primary_filename}")

        if modified:
            print(f"[ResourceMerge] ini文件已修改，正在写入...")
            self._atomic_write_text(ini_file, "".join(lines))
        else:
            print(f"[ResourceMerge] ini文件无需修改")

        for file_path, display_name in files_to_delete.items():
            try:
                os.remove(file_path)
                print(f"[ResourceMerge] 已删除重复文件: {display_name}")
            except OSError as e:
                print(f"[ResourceMerge] 删除文件失败 {file_path}: {e}")


classes = (
    SSMTNode_PostProcess_ResourceMerge,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
