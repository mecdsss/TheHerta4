from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Mapping


MODIMP_RUNTIME_DIR_NAME = "ModImpRuntime"

MODIMP_PATH_PROPS = (
    "modimp_ib_txt_path",
    "modimp_vb0_buf_path",
    "modimp_t5_buf_path",
    "modimp_weight_buf_path",
    "modimp_frame_buf_path",
    "modimp_vb1_layout_path",
    "modimp_root_vb0_path",
)

MODIMP_COLLECTOR_PROPS = (
    "modimp_collector_group_slot",
    "modimp_collector_t0_hash",
    "modimp_collector_u0_hash",
    "modimp_collector_u1_hash",
    "modimp_collector_collect_key",
    "modimp_collector_finish_condition",
)


def _is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def object_workspace_dir_from_type_dir(type_dir: str) -> str:
    path = Path(str(type_dir or ""))
    if path.name.upper().startswith("TYPE_"):
        return str(path.parent)
    return str(path)


def object_workspace_dir_from_unique(workspace_root: str, workspace_unique_str: str) -> str:
    unique = str(workspace_unique_str or "").strip()
    root = Path(str(workspace_root or ""))
    if not unique:
        return str(root)

    if "." in unique:
        lod_name, bare_name = unique.split(".", 1)
        if lod_name.upper().startswith("LOD") and lod_name[3:].isdigit() and bare_name:
            return str(root / lod_name / bare_name)

    return str(root / unique)


def prefix_identity_matches(target_identity: tuple[str, str], source_identity: tuple[str, str]) -> bool:
    target_lod, target_bare = target_identity
    source_lod, source_bare = source_identity
    if target_bare != source_bare:
        return False
    if target_lod:
        return source_lod in {"", target_lod}
    return source_lod in {"", "lod0"}


def _safe_label(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", str(label or "")).strip("_")


def _destination_for(
    runtime_dir: Path,
    source_path: Path,
    *,
    prop_name: str,
    used_names: dict[str, Path],
) -> Path:
    filename = source_path.name or "runtime_file"
    normalized_source = source_path.resolve()
    existing_source = used_names.get(filename)
    if existing_source is None or existing_source == normalized_source:
        used_names[filename] = normalized_source
        return runtime_dir / filename

    stem = source_path.stem or "runtime_file"
    suffix = source_path.suffix
    label = _safe_label(prop_name) or "path"
    for index in range(1, 1000):
        candidate = f"{label}_{stem}{'' if index == 1 else '_' + str(index)}{suffix}"
        existing_source = used_names.get(candidate)
        if existing_source is None or existing_source == normalized_source:
            used_names[candidate] = normalized_source
            return runtime_dir / candidate

    used_names[filename] = normalized_source
    return runtime_dir / filename


def localize_runtime_path_props(
    path_props: Mapping[str, str],
    object_workspace_dir: str,
    *,
    runtime_dir_name: str = MODIMP_RUNTIME_DIR_NAME,
) -> dict[str, str]:
    object_dir = Path(str(object_workspace_dir or ""))
    if not object_dir:
        return dict(path_props)

    try:
        object_dir_resolved = object_dir.resolve()
    except OSError:
        return dict(path_props)

    runtime_dir = object_dir / runtime_dir_name
    localized: dict[str, str] = {}
    used_names: dict[str, Path] = {}

    for prop_name, raw_path in path_props.items():
        source_text = str(raw_path or "").strip()
        if not source_text:
            continue

        source_path = Path(source_text)
        if not source_path.is_file():
            localized[prop_name] = source_text
            continue

        source_resolved = source_path.resolve()
        if _is_relative_to(source_resolved, object_dir_resolved):
            localized[prop_name] = str(source_path)
            continue

        runtime_dir.mkdir(parents=True, exist_ok=True)
        destination = _destination_for(
            runtime_dir,
            source_path,
            prop_name=prop_name,
            used_names=used_names,
        )
        destination_resolved = destination.resolve()
        if destination_resolved != source_resolved:
            shutil.copy2(source_resolved, destination_resolved)
        localized[prop_name] = str(destination)

    return localized
