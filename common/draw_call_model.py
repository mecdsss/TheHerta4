from dataclasses import dataclass, field
from typing import Optional

from .m_key import M_Key
from .object_prefix_helper import ObjectPrefixHelper


@dataclass
class DrawCallModel:
    """DrawCall data parsed from an exported object name."""

    obj_name: str
    source_obj_name: str = ""

    match_draw_ib: str = field(init=False, repr=False, default="")
    match_index_count: str = field(init=False, repr=False, default="")
    match_first_index: str = field(init=False, repr=False, default="")
    match_unique_str: str = field(init=False, repr=False, default="")
    comment_alias_name: str = field(init=False, repr=False, default="")

    work_key_list: list[M_Key] = field(init=False, repr=False, default_factory=list)

    index_count: int = field(init=False, repr=False, default=0)
    vertex_count: int = field(init=False, repr=False, default=0)
    index_offset: int = field(init=False, repr=False, default=0)

    def get_blender_obj_name(self) -> str:
        return self.source_obj_name if self.source_obj_name else self.obj_name

    def __post_init__(self) -> None:
        prefix_info = ObjectPrefixHelper.extract_prefix_info(self.obj_name)
        prefix = prefix_info[0] if prefix_info else self.obj_name
        prefix_parts = ObjectPrefixHelper.parse_prefix_parts(prefix)

        self.match_draw_ib = prefix_parts["draw_ib"]
        self.match_index_count = prefix_parts["index_count"]
        self.match_first_index = prefix_parts["first_index"]
        self.match_unique_str = prefix_parts.get("unique_str", "") or prefix

        if prefix_info:
            _prefix, _separator, base_name = ObjectPrefixHelper.split_name_and_prefix(
                self.obj_name,
                prefix_info[0],
                prefix_info[1],
            )
            self.comment_alias_name = base_name
        else:
            self.comment_alias_name = ""

        if not self.match_unique_str and self.match_draw_ib and self.match_index_count and self.match_first_index:
            self.match_unique_str = self.match_draw_ib + "-" + self.match_index_count + "-" + self.match_first_index

    def get_unique_str(self) -> str:
        if self.match_unique_str:
            return self.match_unique_str
        return self.match_draw_ib + "-" + self.match_index_count + "-" + self.match_first_index

    def get_condition_str(self) -> str:
        if len(self.work_key_list) == 0:
            return ""

        condition_str_list = []
        for i, work_key in enumerate(self.work_key_list):
            condition = work_key.key_name + " == " + str(work_key.tmp_value)

            if i == 0:
                condition_str_list.append(condition)
            else:
                operator = getattr(work_key, "condition_operator", "&&")
                condition_str_list.append(f"{operator} {condition}")

        return " ".join(condition_str_list)

    def get_drawindexed_str(self, obj_name_draw_offset_dict: Optional[dict[str, int]] = None) -> str:
        draw_offset = self.index_offset if obj_name_draw_offset_dict is None else obj_name_draw_offset_dict.get(
            self.obj_name,
            self.index_offset,
        )
        return f"drawindexed = {self.index_count},{draw_offset},0"

    def get_drawindexed_instanced_str(self, obj_name_draw_offset_dict: Optional[dict[str, int]] = None) -> str:
        draw_offset = self.index_offset if obj_name_draw_offset_dict is None else obj_name_draw_offset_dict.get(
            self.obj_name,
            self.index_offset,
        )
        return f"drawindexedinstanced = {self.index_count},INSTANCE_COUNT,{draw_offset},0,FIRST_INSTANCE"
