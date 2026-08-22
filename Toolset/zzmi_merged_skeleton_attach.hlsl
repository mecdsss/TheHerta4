// ZZMI 骨骼合并 - 合并骨架 attach CS（绝区零 deform pass 专用，TheHerta4 生成）
//
// 数据布局（FrameAnalysis 实测，详见 ZZMI骨骼合并计划书.md）：
// - 游戏每部件 palette 是 48 字节/骨骼的结构化 buffer（4x3 矩阵 = 12 floats），
//   由 CPU 在该部件 deform pass（pointlist + SO 蒙皮）前 Map 上传，
//   ring buffer 复用，仅当帧有效。
// - 合并骨架为全部件 palette 的连续拼接（每部件占 [attach_offset, +attach_count)），
//   顶点组直接使用全局骨骼 id（跨部件权重合法）。
//
// 调用时序（统一延迟一帧 = 无相对延迟）：
// 本 CS 在该部件的 deform draw 之后执行——蒙皮读取的合并骨架始终是
// 「上一帧完整 attach 完毕」的版本（全部件同帧、帧内严格一致）；
// 本帧 attach 的数据供下一帧使用。首帧合并骨架全零（一帧后自愈）。

struct ZZBone3x4
{
    float4 r0;
    float4 r1;
    float4 r2;
};

// 当前 deform pass 的 palette（调用方在把 vs-t0 换绑到合并骨架之前保存到 cs-t0）
StructuredBuffer<ZZBone3x4> src_palette : register(t0);
// 合并骨架（同时作为 SRV 换绑到 deform pass 的 vs-t0 供蒙皮读取）
RWStructuredBuffer<ZZBone3x4> merged_skeleton : register(u0);

// 3Dmigoto ini 参数纹理（x1 = attach_offset, y1 = attach_count）
Texture1D<float4> IniParams : register(t120);
#define ZZ_ATTACH_OFFSET IniParams[1].x
#define ZZ_ATTACH_COUNT  IniParams[1].y

[numthreads(64, 1, 1)]
void main(uint3 dispatch_id : SV_DispatchThreadID)
{
    uint bone_index = dispatch_id.x;
    uint count = (uint)ZZ_ATTACH_COUNT;
    if (bone_index >= count)
    {
        return;
    }
    merged_skeleton[(uint)ZZ_ATTACH_OFFSET + bone_index] = src_palette[bone_index];
}
