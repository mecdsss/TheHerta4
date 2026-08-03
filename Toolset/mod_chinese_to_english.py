# -*- coding: utf-8 -*-
"""Translate non-ASCII INI text and referenced texture filenames.

Place the built executable in a MOD directory and run it there.  Directory
names and INI filenames are deliberately never renamed.
"""

from __future__ import annotations

import argparse
import codecs
import hashlib
import os
import re
import shutil
import sys
import tempfile
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable


TEXTURE_EXTENSIONS = {
    ".bmp", ".dds", ".gif", ".jpeg", ".jpg", ".png", ".tga", ".tif",
    ".tiff", ".webp",
}
NON_ASCII_RUN_RE = re.compile(r"[^\x00-\x7f]+")
FILENAME_LINE_RE = re.compile(
    r"^(?P<head>\s*filename\s*=\s*)(?P<rhs>.*?)(?P<newline>\r\n|\n|\r)?$",
    re.IGNORECASE,
)
PROTECTED_PATH_LINE_RE = re.compile(
    r"^(?P<head>\s*(?:include|exclude|vs|ps|hs|ds|gs|cs)\s*=\s*)"
    r"(?P<rhs>.*?)(?P<newline>\r\n|\n|\r)?$",
    re.IGNORECASE,
)


class ConversionError(RuntimeError):
    pass


@dataclass(frozen=True)
class DecodedIni:
    path: Path
    raw: bytes
    text: str
    encoding: str


@dataclass(frozen=True)
class TextureReference:
    ini_path: Path
    source_path: Path


@dataclass(frozen=True)
class MissingTextureReference:
    ini_path: Path
    path_text: str
    resolved_path: Path


@dataclass
class ConversionPlan:
    root: Path
    ini_files: list[DecodedIni] = field(default_factory=list)
    renames: dict[Path, Path] = field(default_factory=dict)
    rewritten_ini: dict[Path, bytes] = field(default_factory=dict)
    affected_references: list[TextureReference] = field(default_factory=list)
    skipped_missing_references: list[MissingTextureReference] = field(default_factory=list)

    @property
    def changed_ini_count(self) -> int:
        return len(self.rewritten_ini)


def latin_token_for_text(text: str) -> str:
    """Match the material-to-resource node's stable English token logic."""
    digest = hashlib.md5(str(text).encode("utf-8")).digest()
    value = int.from_bytes(digest[:8], "big")
    letters = []
    for _ in range(10):
        letters.append(chr(ord("a") + (value % 26)))
        value //= 26
    return "".join(letters).capitalize()


def replace_non_ascii_runs(text: str) -> str:
    return NON_ASCII_RUN_RE.sub(lambda match: latin_token_for_text(match.group(0)), text)


def decode_ini(path: Path) -> DecodedIni:
    raw = path.read_bytes()
    candidates: list[str]
    if raw.startswith(codecs.BOM_UTF8):
        candidates = ["utf-8-sig"]
    elif raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        candidates = ["utf-16"]
    else:
        candidates = ["utf-8", "gb18030"]

    for encoding in candidates:
        try:
            return DecodedIni(path=path, raw=raw, text=raw.decode(encoding), encoding=encoding)
        except UnicodeDecodeError:
            continue
    raise ConversionError(f"无法识别 INI 编码: {path}")


def _split_inline_comment(rhs: str) -> tuple[str, str]:
    quote = ""
    for index, char in enumerate(rhs):
        if char in "\"'":
            if not quote:
                quote = char
            elif quote == char:
                quote = ""
        elif char == ";" and not quote:
            return rhs[:index], rhs[index:]
    return rhs, ""


def _unwrap_path(value: str) -> tuple[str, str, str]:
    leading = value[: len(value) - len(value.lstrip())]
    trailing = value[len(value.rstrip()):]
    core = value.strip()
    if len(core) >= 2 and core[0] in "\"'" and core[-1] == core[0]:
        return leading + core[0], core[1:-1], core[-1] + trailing
    return leading, core, trailing


def _replace_path_basename(path_text: str, new_basename: str) -> str:
    separator_index = max(path_text.rfind("/"), path_text.rfind("\\"))
    return path_text[: separator_index + 1] + new_basename


def _resolve_reference(ini_path: Path, path_text: str, root: Path) -> Path:
    native_path = path_text.replace("\\", os.sep).replace("/", os.sep)
    candidate = Path(native_path)
    if not candidate.is_absolute():
        candidate = ini_path.parent / candidate
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ConversionError(
            f"INI 引用越出 MOD 目录，已拒绝处理: {ini_path} -> {path_text}"
        ) from exc
    return resolved


def _parse_filename_line(line: str) -> tuple[re.Match[str], str, str, str, str] | None:
    match = FILENAME_LINE_RE.match(line)
    if not match:
        return None
    value, comment = _split_inline_comment(match.group("rhs"))
    prefix, path_text, suffix = _unwrap_path(value)
    return match, prefix, path_text, suffix, comment


def _discover_ini_files(root: Path) -> list[Path]:
    return sorted(
        (path for path in root.rglob("*") if path.is_file() and path.suffix.lower() == ".ini"),
        key=lambda path: str(path).casefold(),
    )


def _choose_target(source: Path, occupied: set[str]) -> Path:
    translated_name = replace_non_ascii_runs(source.name)
    candidate = source.with_name(translated_name)
    stem, suffix = candidate.stem, candidate.suffix
    index = 2
    while str(candidate).casefold() in occupied and candidate != source:
        candidate = candidate.with_name(f"{stem}_{index}{suffix}")
        index += 1
    occupied.add(str(candidate).casefold())
    return candidate


def build_plan(root: Path) -> ConversionPlan:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ConversionError(f"不是文件夹: {root}")

    plan = ConversionPlan(root=root)
    plan.ini_files = [decode_ini(path) for path in _discover_ini_files(root)]
    occupied = {
        str(path.resolve(strict=False)).casefold()
        for path in root.rglob("*")
        if path.is_file()
    }

    for ini in plan.ini_files:
        for line in ini.text.splitlines(keepends=True):
            parsed = _parse_filename_line(line)
            if not parsed:
                continue
            _, _, path_text, _, _ = parsed
            if not path_text or Path(path_text.replace("\\", "/")).suffix.lower() not in TEXTURE_EXTENSIONS:
                continue
            basename = re.split(r"[/\\]", path_text)[-1]
            if not NON_ASCII_RUN_RE.search(basename):
                continue
            source = _resolve_reference(ini.path, path_text, root)
            if not source.is_file():
                missing = MissingTextureReference(ini.path, path_text, source)
                if missing not in plan.skipped_missing_references:
                    plan.skipped_missing_references.append(missing)
                continue
            if source not in plan.renames:
                plan.renames[source] = _choose_target(source, occupied)
            plan.affected_references.append(TextureReference(ini.path, source))

    for ini in plan.ini_files:
        output_lines = []
        for line in ini.text.splitlines(keepends=True):
            parsed = _parse_filename_line(line)
            if not parsed:
                path_match = PROTECTED_PATH_LINE_RE.match(line)
                if path_match:
                    value, comment = _split_inline_comment(path_match.group("rhs"))
                    output_lines.append(
                        replace_non_ascii_runs(path_match.group("head"))
                        + value
                        + replace_non_ascii_runs(comment)
                        + (path_match.group("newline") or "")
                    )
                    continue
                output_lines.append(replace_non_ascii_runs(line))
                continue

            match, prefix, path_text, suffix, comment = parsed
            new_path_text = path_text
            basename = re.split(r"[/\\]", path_text)[-1] if path_text else ""
            is_renamed_texture = (
                Path(path_text.replace("\\", "/")).suffix.lower() in TEXTURE_EXTENSIONS
                and bool(NON_ASCII_RUN_RE.search(basename))
            )
            if is_renamed_texture:
                referenced = _resolve_reference(ini.path, path_text, root)
                target = plan.renames.get(referenced)
                if target is not None:
                    new_path_text = _replace_path_basename(path_text, target.name)

            # Paths are protected so directory names and non-texture resources stay valid.
            output_lines.append(
                replace_non_ascii_runs(match.group("head"))
                + prefix
                + new_path_text
                + suffix
                + replace_non_ascii_runs(comment)
                + (match.group("newline") or "")
            )

        new_raw = "".join(output_lines).encode(ini.encoding)
        if new_raw != ini.raw:
            plan.rewritten_ini[ini.path] = new_raw

    _validate_plan(plan)
    return plan


def _validate_plan(plan: ConversionPlan) -> None:
    targets: dict[str, Path] = {}
    for source, target in plan.renames.items():
        if source == target:
            raise ConversionError(f"中文贴图未得到英文名称: {source}")
        key = str(target).casefold()
        previous = targets.get(key)
        if previous is not None and previous != source:
            raise ConversionError(f"多个贴图会被改成同一名称: {previous} / {source} -> {target}")
        targets[key] = source
        if target.exists() and target != source:
            raise ConversionError(f"贴图目标已存在，拒绝覆盖: {target}")

    for reference in plan.affected_references:
        if reference.source_path not in plan.renames:
            raise ConversionError(f"贴图引用未进入改名计划: {reference.source_path}")


def _backup_ini_files(plan: ConversionPlan) -> Path | None:
    if not plan.rewritten_ini:
        return None
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_root = plan.root / ".mod_ini_english_backup" / timestamp
    for path in plan.rewritten_ini:
        relative = path.relative_to(plan.root)
        backup_path = backup_root / relative.parent / f"{relative.name}.bak"
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup_path)
    return backup_root


def _atomic_write(path: Path, data: bytes) -> None:
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _post_validate(plan: ConversionPlan) -> None:
    resolved_by_ini: dict[Path, set[Path]] = {}
    skipped_missing = {
        (item.ini_path, item.resolved_path)
        for item in plan.skipped_missing_references
    }
    for ini_path in {item.ini_path for item in plan.affected_references}:
        decoded = decode_ini(ini_path)
        resolved_references: set[Path] = set()
        for line in decoded.text.splitlines(keepends=True):
            parsed = _parse_filename_line(line)
            if not parsed:
                continue
            _, _, path_text, _, _ = parsed
            if Path(path_text.replace("\\", "/")).suffix.lower() not in TEXTURE_EXTENSIONS:
                continue
            resolved = _resolve_reference(ini_path, path_text, plan.root)
            if (ini_path, resolved) in skipped_missing:
                continue
            if NON_ASCII_RUN_RE.search(re.split(r"[/\\]", path_text)[-1]):
                raise ConversionError(f"校验失败，贴图文件名仍包含中文: {ini_path} -> {path_text}")
            resolved_references.add(resolved)
        resolved_by_ini[ini_path] = resolved_references

    for reference in plan.affected_references:
        target = plan.renames[reference.source_path]
        if target not in resolved_by_ini.get(reference.ini_path, set()):
            raise ConversionError(
                f"校验失败，INI 未指向改名后的贴图: {reference.ini_path} -> {target}"
            )

    for source, target in plan.renames.items():
        if source.exists() or not target.is_file():
            raise ConversionError(f"校验失败，贴图改名状态不正确: {source} -> {target}")


def apply_plan(plan: ConversionPlan, dry_run: bool = False) -> Path | None:
    if dry_run or (not plan.rewritten_ini and not plan.renames):
        return None

    backup_root = _backup_ini_files(plan)
    original_ini = {ini.path: ini.raw for ini in plan.ini_files if ini.path in plan.rewritten_ini}
    staged: dict[Path, Path] = {}
    completed: dict[Path, Path] = {}
    try:
        for source in plan.renames:
            temp_path = source.with_name(f".{source.name}.{os.urandom(8).hex()}.rename_tmp")
            source.rename(temp_path)
            staged[source] = temp_path
        for source, target in plan.renames.items():
            staged[source].rename(target)
            completed[source] = target
        for path, raw in plan.rewritten_ini.items():
            _atomic_write(path, raw)
        _post_validate(plan)
        return backup_root
    except Exception as exc:
        for path, raw in original_ini.items():
            try:
                _atomic_write(path, raw)
            except Exception:
                pass
        for source, target in reversed(list(completed.items())):
            try:
                target.rename(source)
            except Exception:
                pass
        for source, temp_path in reversed(list(staged.items())):
            if temp_path.exists():
                try:
                    temp_path.rename(source)
                except Exception:
                    pass
        raise ConversionError(f"执行失败，已尝试回滚: {exc}") from exc


def _relative(path: Path, root: Path) -> str:
    return str(path.relative_to(root))


def print_plan(plan: ConversionPlan, dry_run: bool) -> None:
    mode = "预览" if dry_run else "执行"
    print(f"[{mode}] MOD 目录: {plan.root}")
    print(f"扫描到 INI: {len(plan.ini_files)}，需修改: {plan.changed_ini_count}")
    print(f"需改名贴图: {len(plan.renames)}")
    for source, target in plan.renames.items():
        print(f"  {_relative(source, plan.root)} -> {_relative(target, plan.root)}")
    if plan.skipped_missing_references:
        print(f"跳过缺失贴图引用: {len(plan.skipped_missing_references)}")
        for reference in plan.skipped_missing_references:
            print(f"  {_relative(reference.ini_path, plan.root)} -> {reference.path_text}")


def _default_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="递归将 MOD 的 INI 中文内容和中文贴图文件名转换为英文标识")
    parser.add_argument("--root", type=Path, default=_default_root(), help="MOD 根目录；默认是程序所在目录")
    parser.add_argument("--dry-run", action="store_true", help="只显示计划，不修改文件")
    parser.add_argument("--no-pause", action="store_true", help="结束时不等待按键")
    args = parser.parse_args(list(argv) if argv is not None else None)

    exit_code = 0
    try:
        plan = build_plan(args.root)
        print_plan(plan, args.dry_run)
        backup_root = apply_plan(plan, dry_run=args.dry_run)
        if args.dry_run:
            print("预览完成，未修改任何文件。")
        else:
            print("处理及引用校验完成。")
            if backup_root:
                print(f"INI 备份: {backup_root}")
    except Exception as exc:
        exit_code = 1
        print(f"错误: {exc}", file=sys.stderr)
        if os.environ.get("MOD_INI_ENGLISH_DEBUG") == "1":
            traceback.print_exc()

    should_pause = getattr(sys, "frozen", False) and not args.no_pause and len(sys.argv) == 1
    if should_pause:
        try:
            input("按 Enter 关闭窗口...")
        except EOFError:
            pass
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
