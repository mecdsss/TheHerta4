import math
import bpy

from mathutils import *


class AlgorithmUtils:
    """平滑法线算法工具类（用于 GI/HI3/HSR/ZZZ/WWMI 等游戏）"""
    @classmethod
    def vector_cross_product(cls,v1,v2):
        """计算两个向量的叉积"""

        return Vector((v1.y*v2.z-v2.y*v1.z,v1.z*v2.x-v2.z*v1.x,v1.x*v2.y-v2.x*v1.y))
    
    @classmethod
    def vector_dot_product (cls,a,b):
        return a.x*b.x+a.y*b.y+a.z*b.z
    
    @classmethod
    def vector_calc_length(cls,v):
        return math.sqrt(v.x*v.x+v.y*v.y+v.z*v.z)
    
    @classmethod
    def vector_normalize(cls,v):
        '''
        归一化 (Normalization): 
        之后对叉乘结果进行归一化（normalize），即调整法线向量的长度为1，这样可以确保法线向量只表示方向而不带有长度信息。
        这一步很重要，因为光照计算通常依赖于单位长度的法线向量来保证正确性。
        '''
        L = cls.vector_calc_length(v)
        if L != 0 :
            return v/L
        return 0
    
    @classmethod
    def vector_to_string(cls,v):
        """将向量转换为字符串，方便用作字典键"""

        return "x=" + str(v.x) + ",y=" + str(v.y) + ",z=" + str(v.z)
    
    @classmethod
    def need_outline(cls,vertex):
        """判断顶点是否需要轮廓线（仅测试用）"""

        need = False
        for g in vertex.groups:
            if g.group == 446:
                need = True
                break
        return True
    
    @classmethod
    def calculate_angle_between_vectors (cls,v1,v2):
        """计算两个向量之间的夹角"""
        
        ASIZE = cls.vector_calc_length(v1)
        BSIZE = cls.vector_calc_length(v2)
        D = ASIZE*BSIZE
        if D != 0:
            degree = math.acos(cls.vector_dot_product(v1,v2)/(ASIZE*BSIZE))
            #S = ASIZE*BSIZE*math.sin(degree)
            return degree
        return 0
    
    @classmethod
    def smooth_normal_save_to_uv(cls):
        """将平滑法线数据保存到 UV 贴图中（用于游戏引擎的平滑法线传递）"""
        
        mesh = bpy.context.active_object.data
        uvdata = mesh.uv_layers.active.data
        
        mesh.calc_tangents(uvmap="TEXCOORD.xy")
        # mesh.calc_tangents()

        co_str_data_dict = {}

        # 开始
        for vertex in mesh.vertices:
            co = vertex.co
            co_str = cls.vector_to_string(co)
            co_str_data_dict[co_str] = []
        print("========")

        for poly in mesh.polygons:
            # 获取三角形的三个顶点
            loop_0 = mesh.loops[poly.loop_start]
            loop_1 = mesh.loops[poly.loop_start+1]
            loop_2 = mesh.loops[poly.loop_start + 2]

            # 获取顶点数据
            vertex_loop0 = mesh.vertices[loop_0.vertex_index]
            vertex_loop1 = mesh.vertices[loop_1.vertex_index]
            vertex_loop2 = mesh.vertices[loop_2.vertex_index]

            # 顶点数据转换为字符串格式
            co0_str = cls.vector_to_string(vertex_loop0.co)
            co1_str = cls.vector_to_string(vertex_loop1.co)
            co2_str = cls.vector_to_string(vertex_loop2.co)

            # 使用CorssProduct计算法线
            normal_vector = cls.vector_cross_product(vertex_loop1.co-vertex_loop0.co,vertex_loop2.co-vertex_loop0.co)
            # 法线归一化使其长度保持为1
            normal_vector = cls.vector_normalize(normal_vector)

            if co0_str in co_str_data_dict and cls.need_outline(vertex_loop0):
                w = cls.calculate_angle_between_vectors(vertex_loop2.co-vertex_loop0.co,vertex_loop1.co-vertex_loop0.co)
                co_str_data_dict[co0_str].append({"n":normal_vector,"w":w,"l":loop_0})
            if co1_str in co_str_data_dict and cls.need_outline(vertex_loop1):
                w = cls.calculate_angle_between_vectors(vertex_loop2.co-vertex_loop1.co,vertex_loop0.co-vertex_loop1.co)
                co_str_data_dict[co1_str].append({"n":normal_vector,"w":w,"l":loop_1})
            if co2_str in co_str_data_dict and cls.need_outline(vertex_loop0):
                w = cls.calculate_angle_between_vectors(vertex_loop1.co-vertex_loop2.co,vertex_loop0.co-vertex_loop2.co)
                co_str_data_dict[co2_str].append({"n":normal_vector,"w":w,"l":loop_2})

        # 存入UV
        uv_layer = mesh.uv_layers.new(name="SmoothNormalMap")
        for poly in mesh.polygons:
            for loop_index in range(poly.loop_start,poly.loop_start+poly.loop_total):
                vertex_index=mesh.loops[loop_index].vertex_index
                vertex = mesh.vertices[vertex_index]

                # 初始化平滑法线和平滑权重
                smoothnormal=Vector((0,0,0))
                weight = 0

                # 基于相邻面的法线加权平均计算平滑法线
                if cls.need_outline(vertex):
                    costr=cls.vector_to_string(vertex.co)

                    if costr in co_str_data_dict:
                        a = co_str_data_dict[costr]
                        # 对于共享此顶点的所有面的数据，遍历它们
                        for d in a:
                            # 分别获取面的法线和权重
                            normal_vector=d['n']
                            w = d['w']
                            # 累加加权法线和权重
                            smoothnormal  += normal_vector*w
                            weight  += w
                if smoothnormal != Vector((0,0,0)):
                    smoothnormal /= weight
                    smoothnormal = cls.vector_normalize(smoothnormal)

                loop_normal = mesh.loops[loop_index].normal
                loop_tangent = mesh.loops[loop_index].tangent
                loop_bitangent = mesh.loops[loop_index].bitangent

                tx = cls.vector_dot_product(loop_tangent,smoothnormal)
                ty = cls.vector_dot_product(loop_bitangent,smoothnormal)
                tz = cls.vector_dot_product(loop_normal,smoothnormal)

                normalT=Vector((tx,ty,tz))
                # print("nor:",smoothnormal)

                # 将法线XY分量存储到UV贴图的坐标 (X:法线x, Y:法线y)
                # 需要根据实际调整，例如UE为（x,1+y）

                # uv = (normalT.x, 1 + normalT.y) 
                uv = (normalT.x, 1 + normalT.y) 
                uv_layer.data[loop_index].uv = uv

        # 重新计算物体的UV贴图以应用更改
        # bpy.ops.object.mode_set(mode="EDIT")
        # bpy.ops.uv.unwrap(method='ANGLE_BASED')
        # bpy.ops.object.mode_set(mode="OBJECT")