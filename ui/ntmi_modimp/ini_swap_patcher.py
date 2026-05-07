from __future__ import annotations

import re
from pathlib import Path


SECTION_RE = re.compile(r"^\[(?P<name>[^\]]+)\]\s*$")
MESH_COMMENT_RE = re.compile(r"^;\s*\[mesh:(?P<object_name>[^\]]+)\]")
DRAWINDEXED_RE = re.compile(r"^(?P<indent>\s*)drawindexed\s*=")


def _find_section_ranges(lines: list[str]) -> list[tuple[str, int, int]]:
    sections = []
    current_name = ""
    current_start = -1
    for index, line in enumerate(lines):
        match = SECTION_RE.match(line.strip())
        if not match:
            continue
        if current_name:
            sections.append((current_name, current_start, index))
        current_name = match.group("name")
        current_start = index
    if current_name:
        sections.append((current_name, current_start, len(lines)))
    return sections


def _section_exists(lines: list[str], section_name: str) -> bool:
    target = section_name.casefold()
    return any(name.casefold() == target for name, _start, _end in _find_section_ranges(lines))


def _insert_into_section(lines: list[str], section_name: str, insert_lines: list[str]) -> bool:
    target = section_name.casefold()
    for name, _start, end in _find_section_ranges(lines):
        if name.casefold() != target:
            continue
        existing = {line.strip() for line in lines}
        filtered = [line for line in insert_lines if line.strip() and line.strip() not in existing]
        if not filtered:
            return True
        insert_at = end
        while insert_at > 0 and not lines[insert_at - 1].strip():
            insert_at -= 1
        lines[insert_at:insert_at] = filtered
        return True
    return False


def _append_block(lines: list[str], block: list[str]):
    if lines and lines[-1].strip():
        lines.append("")
    lines.extend(block)
    if lines and lines[-1].strip():
        lines.append("")


def _ensure_constants(lines: list[str], swap_nodes: list[dict[str, object]]):
    insert_lines = ["global $active0 = 0"]
    for node in swap_nodes:
        variable_name = str(node["variable_name"])
        insert_lines.append(f"global persist {variable_name} = 0")

    if not _insert_into_section(lines, "Constants", insert_lines):
        _append_block(lines, ["[Constants]", *insert_lines])


def _ensure_present(lines: list[str]):
    insert_lines = ["post $active0 = 0"]
    if not _insert_into_section(lines, "Present", insert_lines):
        _append_block(lines, ["[Present]", *insert_lines])


def _ensure_key_swap_sections(lines: list[str], swap_nodes: list[dict[str, object]]):
    for node in swap_nodes:
        section_name = str(node["section_name"])
        if _section_exists(lines, section_name):
            continue
        option_sequence = ",".join(str(index) for index in range(int(node["option_count"])))
        block = [f"[{section_name}]"]
        comment = str(node.get("comment", "") or "").strip()
        if comment:
            block.append(f"; {comment}")
        block.extend(
            [
                "condition = $active0 == 1",
                f"key = {node['hotkey']}",
                f"type = {node['swap_type']}",
                f"{node['variable_name']} = {option_sequence},",
            ]
        )
        _append_block(lines, block)


def _inject_active_flag(lines: list[str]):
    result = []
    for index, line in enumerate(lines):
        result.append(line)
        stripped = line.strip()
        if not stripped.startswith("[TextureOverride"):
            continue
        if index + 1 < len(lines) and lines[index + 1].strip() == "$active0 = 1":
            continue
        result.append("$active0 = 1")
    return result


def _condition_for_object(object_name: str, object_conditions: dict[str, str]) -> str:
    if object_name in object_conditions:
        return object_conditions[object_name]
    if object_name.endswith("_copy"):
        base_name = object_name[:-5]
        if base_name in object_conditions:
            return object_conditions[base_name]
    return ""


def _wrap_drawindexed(lines: list[str], object_conditions: dict[str, str]) -> list[str]:
    result = []
    current_object_name = ""
    for line in lines:
        comment_match = MESH_COMMENT_RE.match(line.strip())
        if comment_match:
            current_object_name = comment_match.group("object_name")
            result.append(line)
            continue

        if DRAWINDEXED_RE.match(line) and current_object_name:
            condition = _condition_for_object(current_object_name, object_conditions)
            if condition:
                previous_line = result[-1].strip() if result else ""
                if previous_line.startswith("if ") and condition in previous_line:
                    result.append(line)
                    continue
                indent = DRAWINDEXED_RE.match(line).group("indent")
                result.append(f"{indent}if {condition}")
                result.append(f"{indent}  {line.strip()}")
                result.append(f"{indent}endif")
                continue

        result.append(line)
    return result


def patch_ini_file(
    ini_path: str | Path,
    *,
    swap_nodes: list[dict[str, object]],
    object_conditions: dict[str, str],
) -> bool:
    path = Path(ini_path)
    if not path.is_file():
        return False

    original_text = path.read_text(encoding="utf-8")
    lines = original_text.splitlines()

    if swap_nodes:
        _ensure_constants(lines, swap_nodes)
        _ensure_present(lines)
        _ensure_key_swap_sections(lines, swap_nodes)
        lines = _inject_active_flag(lines)

    if object_conditions:
        lines = _wrap_drawindexed(lines, object_conditions)

    new_text = "\n".join(lines).rstrip() + "\n"
    if new_text == original_text:
        return False
    path.write_text(new_text, encoding="utf-8")
    return True
