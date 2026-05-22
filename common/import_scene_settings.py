import bpy


def apply_import_render_environment(scene=None):
    target_scene = scene or getattr(bpy.context, "scene", None)
    if target_scene is None:
        return

    render = getattr(target_scene, "render", None)
    if render is not None and hasattr(render, "film_transparent"):
        try:
            render.film_transparent = True
        except Exception:
            pass

    display_settings = getattr(target_scene, "display_settings", None)
    if display_settings is not None and hasattr(display_settings, "display_device"):
        try:
            display_settings.display_device = "sRGB"
        except Exception:
            pass

    view_settings = getattr(target_scene, "view_settings", None)
    if view_settings is not None and hasattr(view_settings, "view_transform"):
        try:
            view_settings.view_transform = "Standard"
        except Exception:
            pass

    world = getattr(target_scene, "world", None)
    if world is not None and hasattr(world, "color"):
        try:
            world.color = (1.0, 1.0, 1.0)
        except Exception:
            pass
    if world is not None and getattr(world, "use_nodes", False):
        node_tree = getattr(world, "node_tree", None)
        if node_tree is not None:
            background = next(
                (node for node in getattr(node_tree, "nodes", []) if getattr(node, "bl_idname", "") == "ShaderNodeBackground"),
                None,
            )
            if background is not None:
                try:
                    background.inputs["Color"].default_value = (1.0, 1.0, 1.0, 1.0)
                except Exception:
                    pass
