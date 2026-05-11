import json
import os
from dataclasses import asdict, dataclass
from typing import Dict, List

from ...utils.format_utils import Fatal


@dataclass
class ExtractedObjectBufferSemantic:
    name: str
    index: int
    format: str
    stride: int = 0

    def __post_init__(self):
        if self.stride == 0:
            self.stride = self.format.byte_width


@dataclass
class ExtractedObjectBuffer:
    semantics: List[ExtractedObjectBufferSemantic]


@dataclass
class ExtractedObjectComponent:
    vertex_offset: int
    vertex_count: int
    index_offset: int
    index_count: int
    vg_offset: int
    vg_count: int
    vg_map: Dict[int, int]


@dataclass
class ExtractedObjectShapeKeys:
    offsets_hash: str = ""
    scale_hash: str = ""
    vertex_count: int = 0
    dispatch_y: int = 0
    checksum: int = 0


@dataclass
class ExtractedObject:
    vb0_hash: str
    cb4_hash: str
    vertex_count: int
    index_count: int
    components: List[ExtractedObjectComponent]
    shapekeys: ExtractedObjectShapeKeys
    export_format: Dict[str, ExtractedObjectBuffer]

    def __post_init__(self):
        if isinstance(self.shapekeys, dict):
            self.components = [ExtractedObjectComponent(**component) for component in self.components]
            self.shapekeys = ExtractedObjectShapeKeys(**self.shapekeys)

    def as_json(self):
        return json.dumps(asdict(self), indent=4)


class ExtractedObjectHelper:
    @classmethod
    def read_metadata(cls, metadata_path: str) -> ExtractedObject:
        if not os.path.exists(metadata_path):
            raise Fatal("无法找到Metadata.json文件，请确认是否存在该文件。")

        with open(metadata_path, encoding="utf-8") as f:
            return ExtractedObject(**json.load(f))

    @classmethod
    def build_from_submesh_metadata_list(cls, metadata_list: list) -> ExtractedObject:
        if not metadata_list:
            raise Fatal("No SubmeshMetadata provided to build ExtractedObject.")

        first_json = metadata_list[0].submesh_json
        components = []
        total_index_count = 0
        max_vertex_end = 0

        for metadata in metadata_list:
            submesh_json = metadata.submesh_json
            vertex_offset = int(submesh_json.VertexOffset)
            vertex_count = max(int(submesh_json.VertexCount), 0)
            index_offset = int(submesh_json.JsonDict.get("IndexOffset", 0))
            index_count = int(submesh_json.JsonDict.get("IndexCount", 0))
            vg_offset = int(submesh_json.VGOffset)
            vg_count = int(submesh_json.VGCount)

            vg_map = {}
            for vg_key, vg_value in dict(submesh_json.VGMap).items():
                try:
                    vg_map[str(vg_key)] = int(vg_value)
                except (TypeError, ValueError):
                    continue

            components.append(ExtractedObjectComponent(
                vertex_offset=vertex_offset,
                vertex_count=vertex_count,
                index_offset=index_offset,
                index_count=index_count,
                vg_offset=vg_offset,
                vg_count=vg_count,
                vg_map=vg_map,
            ))
            total_index_count += index_count
            max_vertex_end = max(max_vertex_end, vertex_offset + vertex_count)

        shapekeys_info = dict(first_json.ShapeKeysInfo)
        shapekeys = ExtractedObjectShapeKeys(
            offsets_hash=shapekeys_info.get("offsets_hash", ""),
            scale_hash=shapekeys_info.get("scale_hash", ""),
            vertex_count=int(shapekeys_info.get("vertex_count", 0)),
            dispatch_y=int(shapekeys_info.get("dispatch_y", 0)),
            checksum=int(shapekeys_info.get("checksum", 0)),
        )

        return ExtractedObject(
            vb0_hash=first_json.VertexLimitVB,
            cb4_hash=first_json.CB4Hash,
            vertex_count=max_vertex_end,
            index_count=total_index_count,
            components=components,
            shapekeys=shapekeys,
            export_format={},
        )
