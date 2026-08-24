// ZZMI 骨骼合并 - 合并骨架 attach CS（绝区零 deform pass 专用，TheHerta4 生成）
//
// 数据布局（FrameAnalysis 实测，详见 ZZMI骨骼合并计划书.md）：
// - 游戏每部件 palette 是 48 字节/骨骼的结构化 buffer（4x3 矩阵 = 12 floats），
//   由 CPU 在该部件 deform pass（pointlist + SO 蒙皮）前 Map 上传，
//   ring buffer 复用，仅当帧有效。
// - 合并骨架为全部件骨骼的并集（槽位 = 全局骨骼 id），顶点组直接使用全局
//   骨骼 id（同骨架组内跨部件共享骨骼合法）。
//
// 调用时序（零延迟逐 pass attach 版，2026-08-25 定案）：
// 本 CS 在该部件的 deform draw **之前**由 deform VB 段 run——cs-t0 是该段
// `ResourceZZPalette_<DrawIB> = copy vs-t0` 刚捕获的**当帧 palette**，
// 按 vg_map 表（cs-t1，局部骨骼 id -> 合并骨架全局槽位）写入本组骨架：
// 本部件引用的全部骨骼（含跨部件共享的 canonical 槽位）此刻即为当帧内容，
// 随后换绑 vs-t0 到本组骨架的 draw 蒙皮读到的就是当帧姿态——与渲染侧
// 当帧绑定矩阵（vs-cb2 角色级绑定表）一致，杜绝"慢一帧 x 当帧绑定"错位。
// [Present] 帧尾再次 run 本 CS 写全部槽位（同帧内容，作为兜底/下一帧基线）。
// 首帧无需自愈（本部件骨骼当帧即写）。

struct ZZBone3x4
{
    float4 r0;
    float4 r1;
    float4 r2;
};

// 当前 deform pass 的 palette（调用方在把 vs-t0 换绑到合并骨架之前保存到 cs-t0）
StructuredBuffer<ZZBone3x4> src_palette : register(t0);
// 局部骨骼 id -> 合并骨架全局槽位 映射表（vg_map；3DMigoto 的
// format=R32G32B32A32_UINT 创建格式化缓冲，故用 Buffer<uint4> 声明——
// 与视图类型精确匹配，读取全量元素；槽位值在 .x，每槽后跟 3 个 0）
Buffer<uint4> vg_map : register(t1);
// 合并骨架（同时作为 SRV 换绑到 deform pass 的 vs-t0 供蒙皮读取）
RWStructuredBuffer<ZZBone3x4> merged_skeleton : register(u0);

// 3Dmigoto ini 参数纹理：本 fork（3Dmigoto-Armor）实测 y1 在 IniParams[1].y
// （2026-08-23 双帧实证：读 [0].y -> count=0 全零不写；读 [1].y -> count=y1 生效）。
// 标准版 3DMigoto 是 IniParams[0]=(x1,y1,z1,w1)，本 fork 布局从 [1] 起。
Texture1D<float4> IniParams : register(t120);
#define ZZ_ATTACH_COUNT IniParams[1].y

[numthreads(64, 1, 1)]
void main(uint3 dispatch_id : SV_DispatchThreadID)
{
    uint bone_index = dispatch_id.x;
    uint count = (uint)ZZ_ATTACH_COUNT;
    if (bone_index >= count)
    {
        return;
    }
    // 按 vg_map 写入全局槽位：本部件引用的骨骼（含共享 canonical）当帧覆盖。
    // vg_map 是 3DMigoto 的 format=R32G32B32A32_UINT 格式化缓冲，用 Buffer<uint4>
    // 声明（与视图精确匹配；StructuredBuffer 声明曾导致只读到第 0 个元素、
    // 其余骨骼全部塌进 slot 0——2026-08-23 新 dump 实证 G3 仅 3 槽非零）。
    uint slot = vg_map[bone_index].x;
    if (slot < 249)
    {
        merged_skeleton[slot] = src_palette[bone_index];
    }
}
