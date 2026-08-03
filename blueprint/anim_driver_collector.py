from collections import deque
from typing import List, Dict, Set, Tuple

from .anim_driver_base import SSMTNode_AnimDriver_Base, SSMTSocketAnimDriver


class AnimationDriverCollector:
    def __init__(self, node_group):
        self.node_group = node_group

    @staticmethod
    def _node_sort_key(node):
        tree_name = str(getattr(getattr(node, "id_data", None), "name", "") or "")
        node_name = str(getattr(node, "name", "") or "")
        return tree_name.casefold(), node_name.casefold(), tree_name, node_name

    def count_paragraphs(self) -> int:
        """轻量方法：只统计段落数，不调用 generate_ini_segment。
        适用于 draw 回调等只读上下文。"""
        driver_nodes = self._find_animation_driver_nodes()
        if not driver_nodes:
            return 0
        graph, node_set = self._build_graph(driver_nodes)
        paragraphs = self._divide_into_paragraphs(graph, node_set)
        return len(paragraphs)

    def collect(self) -> List[Dict]:
        driver_nodes = self._find_animation_driver_nodes()
        if not driver_nodes:
            return []

        graph, node_set = self._build_graph(driver_nodes)
        paragraphs = self._divide_into_paragraphs(graph, node_set)

        seen_runtime_segments = set()
        result = []
        for idx, paragraph_nodes in enumerate(paragraphs):
            ordered_nodes = self._topological_sort(paragraph_nodes, graph)
            ini_content_parts = []
            for node in ordered_nodes:
                connected_upstream = list(graph[node]["inputs"]) if node in graph else []
                try:
                    segment = node.generate_ini_segment(connected_nodes=connected_upstream)

                    if hasattr(node, 'fps') and segment:
                        runtime_key = self._build_runtime_segment_key(node, segment)
                        if runtime_key in seen_runtime_segments:
                            segment = ""
                        else:
                            seen_runtime_segments.add(runtime_key)

                    if segment:
                        ini_content_parts.append(segment)
                except Exception as e:
                    print(f"动画驱动节点收集失败: {node.name} - {e}")

            merged = self._merge_paragraph_sections(ini_content_parts)
            if merged.strip():
                result.append({
                    "paragraph_index": idx,
                    "node_names": [n.name for n in ordered_nodes],
                    "ini_content": merged,
                })

        return result

    @staticmethod
    def _build_runtime_segment_key(node, segment: str) -> tuple:
        return (
            getattr(node, "bl_idname", ""),
            int(getattr(node, "fps", 0) or 0),
            int(getattr(node, "playback_rate", 0) or 0),
            str(segment or "").strip(),
        )

    def _merge_paragraph_sections(self, segments):
        sections = {}
        section_order = []
        current = None

        for segment in segments:
            for line in segment.split('\n'):
                stripped = line.strip()
                if stripped.startswith('[') and stripped.endswith(']'):
                    current = stripped
                    if current not in sections:
                        sections[current] = []
                        section_order.append(current)
                elif current:
                    sections[current].append(line)

        output = []
        for section_name in section_order:
            output.append(section_name)
            output.extend(sections[section_name])

        return '\n'.join(output)

    def _find_animation_driver_nodes(self):
        result = []
        for node in self.node_group.nodes:
            if hasattr(node, "generate_ini_segment") and callable(node.generate_ini_segment):
                try:
                    if node.bl_idname != SSMTNode_AnimDriver_Base.bl_idname:
                        result.append(node)
                except Exception:
                    continue
        return result

    def _build_graph(self, nodes) -> Tuple[Dict, Set]:
        node_set = set(nodes)
        graph = {node: {"inputs": set(), "outputs": set()} for node in nodes}

        for link in self.node_group.links:
            from_node = link.from_node
            to_node = link.to_node
            if from_node not in node_set or to_node not in node_set:
                continue
            from_socket_type = getattr(link.from_socket, "bl_idname", "")
            to_socket_type = getattr(link.to_socket, "bl_idname", "")
            if from_socket_type == 'SSMTSocketAnimDriver' and to_socket_type == 'SSMTSocketAnimDriver':
                graph[from_node]["outputs"].add(to_node)
                graph[to_node]["inputs"].add(from_node)

        return graph, node_set

    def _divide_into_paragraphs(self, graph: Dict, node_set: Set) -> List[List]:
        visited = set()
        paragraphs = []

        for node in sorted(node_set, key=self._node_sort_key):
            if node in visited:
                continue
            component = self._bfs_component(node, graph, visited)
            paragraphs.append(component)

        return paragraphs

    def _bfs_component(self, start_node, graph: Dict, visited: Set) -> List:
        queue = deque([start_node])
        component = []
        visited.add(start_node)

        while queue:
            node = queue.popleft()
            component.append(node)
            neighbors = graph[node]["inputs"] | graph[node]["outputs"]
            for neighbor in sorted(neighbors, key=self._node_sort_key):
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(neighbor)

        return component

    def _topological_sort(self, nodes: List, graph: Dict) -> List:
        if len(nodes) <= 1:
            return nodes

        in_degree = {}
        for node in nodes:
            in_degree[node] = len([n for n in graph[node]["inputs"] if n in set(nodes)])

        queue = [n for n in nodes if in_degree[n] == 0]
        queue.sort(key=self._node_sort_key)
        sorted_nodes = []

        while queue:
            node = queue.pop(0)
            sorted_nodes.append(node)
            for neighbor in sorted(graph[node]["outputs"], key=self._node_sort_key):
                if neighbor in in_degree:
                    in_degree[neighbor] -= 1
                    if in_degree[neighbor] == 0:
                        queue.append(neighbor)
                        queue.sort(key=self._node_sort_key)

        remaining = sorted(
            (n for n in nodes if n not in sorted_nodes),
            key=self._node_sort_key,
        )
        return sorted_nodes + remaining


def register():
    pass


def unregister():
    pass
