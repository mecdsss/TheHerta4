import os
import re

import bpy
from bpy.app.handlers import persistent

from .log_utils import LOG

_CHANGE_CHECK_ACTIVE_INTERVAL_SECONDS = 1.0
_CHANGE_CHECK_IDLE_INTERVAL_SECONDS = 5.0
_CHANGE_CHECK_EMPTY_INTERVAL_SECONDS = 10.0
_FAST_POLL_CYCLES_AFTER_CHANGE = 3
_timer_handle = None
_image_signature_cache = {}
_fast_poll_cycles_remaining = 0


def _get_image_cache_key(image: bpy.types.Image) -> str:
    try:
        return str(image.as_pointer())
    except Exception:
        return image.name


def _get_images_snapshot():
    images = getattr(getattr(bpy, "data", None), "images", None)
    if images is None:
        return None

    try:
        return tuple(images)
    except Exception:
        return None


def _is_reloadable_image(image: bpy.types.Image) -> bool:
    if image is None:
        return False

    if getattr(image, "packed_file", None) is not None:
        return False

    if getattr(image, "is_dirty", False):
        return False

    if getattr(image, "source", "") not in {"FILE", "SEQUENCE", "TILED"}:
        return False

    filepath = str(getattr(image, "filepath", "") or "").strip()
    return bool(filepath)


def _replace_case_insensitive(text: str, token: str, value: str) -> str:
    return re.sub(re.escape(token), value, text, flags=re.IGNORECASE)


def _udim_to_uvtile(tile_number: int) -> str:
    if tile_number < 1001:
        return f"u1_v1"

    offset = tile_number - 1001
    u_value = (offset % 10) + 1
    v_value = (offset // 10) + 1
    return f"u{u_value}_v{v_value}"


def _expand_image_paths(image: bpy.types.Image) -> list[str]:
    raw_path = str(getattr(image, "filepath", "") or "").strip()
    if not raw_path:
        return []

    if getattr(image, "source", "") != "TILED":
        return [raw_path]

    upper_path = raw_path.upper()
    has_udim = "<UDIM>" in upper_path
    has_uvtile = "<UVTILE>" in upper_path
    if not has_udim and not has_uvtile:
        return [raw_path]

    expanded_paths = []
    for tile in getattr(image, "tiles", []):
        tile_number = int(getattr(tile, "number", 0) or 0)
        tile_path = raw_path
        if has_udim:
            tile_path = _replace_case_insensitive(tile_path, "<UDIM>", str(tile_number))
        if has_uvtile:
            tile_path = _replace_case_insensitive(tile_path, "<UVTILE>", _udim_to_uvtile(tile_number))
        expanded_paths.append(tile_path)

    return expanded_paths or [raw_path]


def _resolve_image_path(image: bpy.types.Image, raw_path: str) -> str:
    try:
        resolved_path = bpy.path.abspath(raw_path, library=image.library)
    except Exception:
        resolved_path = raw_path
    return os.path.normcase(os.path.normpath(resolved_path))


def _build_image_signature(image: bpy.types.Image):
    if not _is_reloadable_image(image):
        return None

    signature_parts = []
    unique_paths = []
    for raw_path in _expand_image_paths(image):
        resolved_path = _resolve_image_path(image, raw_path)
        if resolved_path not in unique_paths:
            unique_paths.append(resolved_path)

    for resolved_path in unique_paths:
        try:
            file_stat = os.stat(resolved_path)
            signature_parts.append(
                (
                    resolved_path,
                    int(getattr(file_stat, "st_mtime_ns", int(file_stat.st_mtime * 1_000_000_000))),
                    int(file_stat.st_size),
                )
            )
        except OSError:
            signature_parts.append((resolved_path, None, None))

    return tuple(signature_parts)


def _prime_image_signature_cache():
    global _image_signature_cache

    images = _get_images_snapshot()
    if images is None:
        return False

    cache = {}
    for image in images:
        signature = _build_image_signature(image)
        if signature is None:
            continue
        cache[_get_image_cache_key(image)] = signature
    _image_signature_cache = cache
    return True


def _log_reload_summary(reloaded_names: list[str]):
    if not reloaded_names:
        return

    preview_names = ", ".join(reloaded_names[:5])
    if len(reloaded_names) > 5:
        preview_names += ", ..."
    LOG.info(f"[TextureAutoReload] Reloaded {len(reloaded_names)} image(s): {preview_names}")


def _reload_image(image: bpy.types.Image) -> bool:
    try:
        image.reload()
        return True
    except Exception as exc:
        LOG.warning(f"[TextureAutoReload] Failed to reload '{image.name}': {exc}")
        return False


def _check_and_reload_changed_images():
    global _image_signature_cache

    images = _get_images_snapshot()
    if images is None:
        return 0, 0

    active_cache_keys = set()
    reloaded_names = []
    reloadable_count = 0

    for image in images:
        cache_key = _get_image_cache_key(image)
        active_cache_keys.add(cache_key)

        signature = _build_image_signature(image)
        if signature is None:
            _image_signature_cache.pop(cache_key, None)
            continue
        reloadable_count += 1

        previous_signature = _image_signature_cache.get(cache_key)
        if previous_signature is None:
            _image_signature_cache[cache_key] = signature
            continue

        if previous_signature == signature:
            continue

        if _reload_image(image):
            reloaded_names.append(image.name)

        _image_signature_cache[cache_key] = _build_image_signature(image) or signature

    stale_keys = [cache_key for cache_key in _image_signature_cache.keys() if cache_key not in active_cache_keys]
    for cache_key in stale_keys:
        _image_signature_cache.pop(cache_key, None)

    _log_reload_summary(reloaded_names)
    return reloadable_count, len(reloaded_names)


def texture_auto_reload_timer_callback():
    global _fast_poll_cycles_remaining

    try:
        reloadable_count, reloaded_count = _check_and_reload_changed_images()
    except Exception as exc:
        LOG.warning(f"[TextureAutoReload] Periodic check failed: {exc}")
        return _CHANGE_CHECK_IDLE_INTERVAL_SECONDS

    if reloaded_count > 0:
        _fast_poll_cycles_remaining = _FAST_POLL_CYCLES_AFTER_CHANGE
        return _CHANGE_CHECK_ACTIVE_INTERVAL_SECONDS

    if _fast_poll_cycles_remaining > 0:
        _fast_poll_cycles_remaining -= 1
        return _CHANGE_CHECK_ACTIVE_INTERVAL_SECONDS

    if reloadable_count <= 0:
        return _CHANGE_CHECK_EMPTY_INTERVAL_SECONDS

    return _CHANGE_CHECK_IDLE_INTERVAL_SECONDS


@persistent
def load_post_handler(_scene):
    """加载后处理：初始化贴图签名缓存"""
    _prime_image_signature_cache()


def register():
    global _timer_handle

    if load_post_handler not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(load_post_handler)

    if not _timer_handle or not bpy.app.timers.is_registered(_timer_handle):
        _timer_handle = bpy.app.timers.register(
            texture_auto_reload_timer_callback,
            first_interval=_CHANGE_CHECK_ACTIVE_INTERVAL_SECONDS,
            persistent=True,
        )


def unregister():
    global _timer_handle, _image_signature_cache, _fast_poll_cycles_remaining

    if load_post_handler in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(load_post_handler)

    if _timer_handle and bpy.app.timers.is_registered(_timer_handle):
        bpy.app.timers.unregister(_timer_handle)

    _timer_handle = None
    _image_signature_cache = {}
    _fast_poll_cycles_remaining = 0
