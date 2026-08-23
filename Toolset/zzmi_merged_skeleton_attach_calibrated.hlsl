// ZZMI 骨骼合并 - 合并骨架 attach CS（校准版，绝区零 deform pass 专用，TheHerta4 生成）
//
// 与 zzmi_merged_skeleton_attach.hlsl 的关系：那是"直拷版"（同组同空间）；
// 本文件是"校准版"——把源部件的 palette 骨骼从源组对象空间换算进目标组对象空间后写入。
//
// 数据布局（FrameAnalysis 实测，详见 ZZMI骨骼合并计划书.md）：
// - palette：48 字节/骨骼结构化 buffer（3x4：三行 float4 = (旋转行 xyz, 平移分量 w)），
//   列向量约定 object = Rm * bind + tm；CPU 在该部件 deform pass 前 Map 上传。
// - 渲染 cb1（逐部件常量块）：前 4 个 float4 = 对象→世界矩阵（rows 0-2 旋转行 w=0、
//   row 3 平移 w=1），行向量约定 world = object * R + t（即列向量 U = (R^T, t)）。
//   palette 与其逐物体 1:1 配对：palette 输出对象空间顶点，渲染 VS 用 cb1 摆到世界。
//
// 校准公式（跨组引用外来骨骼时保证各自对象→世界变换正常）：
//   外来骨骼 M（bind→源组空间）给目标组用时必须换成 bind→目标组空间：
//     M' = C × M，C = U_dst^-1 × U_src（目标组空间 ← 源组空间换算器）
//   展开（刚体：旋转行单位正交，R^-1 = R^T；实测无缩放）：
//     C_rot = Rd × Rs^T；C_t = Rd × (t_src - t_dst)
//     Rm' = C_rot × Rm；tm' = C_rot × tm + C_t
//   世界不变性：U_dst × (M' × p) ≡ U_src × (M × p)。
//
// 时序（用户拍板：全部数据同帧，整体延迟一帧，杜绝混帧抖动）：
// 本 CS 在 [Present]（帧尾）统一执行——当帧 palette 副本（deform draw 处
// `copy vs-t0` 成持久资源 ResourceZZPalette_<DrawIB>；ring buffer 同帧内会被
// 后续 pass 重写，别名撑不到帧尾）与当帧 cb1 捕获（渲染 draw 处 copy，last-wins）
// 此刻同时在手，一次写出的骨架全部同属当帧；下一帧各 deform draw 读到的是
// 干净的上一帧完整骨架。
// （被否方案"全部当前帧"在 ZZZ 管线物理不可行：当帧全套 palette 因逐 pass
// Map + ring 复用从不并存；cb1 只在渲染 draw 才绑得到，而渲染块在 deform 块之后。）
// 首帧合并骨架全零（一帧异常后自愈）。
//
// 调用方语义（[Present] 里每个部件 × 每个骨架组各调一次）：
// - cs-t0 = ResourceZZPalette_<DrawIB>（本部件当帧 palette 的持久副本）；
// - cs-cb1 = 本部件所在组的对象→世界 cb1 捕获（ResourceZZCb1_G<本组>，**常量缓冲槽 b1**）；
// - cs-cb2 = 目标组的对象→世界 cb1 捕获（ResourceZZCb1_G<目标组>，**常量缓冲槽 b2**）；
// - cs-u0  = 目标组合并骨架（ResourceZZMergedSkeleton_G<目标组>）；
// - x1 = attach_offset（本部件 palette 在全局骨骼编号中的起始槽位）；
// - y1 = attach_count（本部件骨骼数）。
// 校准是恒等安全的：本组 attach 时 src == dst，C = 单位阵，退化为直拷。

struct ZZBone3x4
{
    float4 r0;
    float4 r1;
    float4 r2;
};

// 本部件当帧 palette 的持久副本（deform draw 处 copy vs-t0 写入；ring buffer 同帧即失效）
StructuredBuffer<ZZBone3x4> src_palette : register(t0);
// 合并骨架（目标组；同时作为 SRV 换绑到该组各 deform pass 的 vs-t0 供蒙皮读取）
RWStructuredBuffer<ZZBone3x4> merged_skeleton : register(u0);

// 源组 / 目标组的对象→世界 cb1 捕获（渲染 draw 处 copy 的逐部件常量块，前 4 float4 有效）
cbuffer ZZCb1Src : register(b1) { float4 zz_cb1_src[4]; }
cbuffer ZZCb1Dst : register(b2) { float4 zz_cb1_dst[4]; }

// 3Dmigoto ini 参数纹理（x1 = attach_offset, y1 = attach_count）
Texture1D<float4> IniParams : register(t120);
#define ZZ_ATTACH_OFFSET IniParams[1].x
#define ZZ_ATTACH_COUNT  IniParams[1].y

// cb1 块形态校验：rows 0-2 w=0、row 3 w=1；不满足（未捕获/首帧/共享数组误入）则退回直拷
bool zz_cb1_valid(float4 r3)
{
    return abs(r3.w - 1.0) < 1e-3;
}

[numthreads(64, 1, 1)]
void main(uint3 dispatch_id : SV_DispatchThreadID)
{
    uint bone_index = dispatch_id.x;
    uint count = (uint)ZZ_ATTACH_COUNT;
    if (bone_index >= count)
    {
        return;
    }

    ZZBone3x4 m = src_palette[bone_index];
    uint slot = (uint)ZZ_ATTACH_OFFSET + bone_index;

    // cb1 任一方无效（未捕获/首帧）-> 直拷兜底（= 分组版行为）
    if (!zz_cb1_valid(zz_cb1_src[3]) || !zz_cb1_valid(zz_cb1_dst[3]))
    {
        merged_skeleton[slot] = m;
        return;
    }

    // 行向量旋转行（cb1 rows 0-2 的 xyz）；平移 = row 3 的 xyz
    float3x3 Rs = float3x3(zz_cb1_src[0].xyz, zz_cb1_src[1].xyz, zz_cb1_src[2].xyz);
    float3x3 Rd = float3x3(zz_cb1_dst[0].xyz, zz_cb1_dst[1].xyz, zz_cb1_dst[2].xyz);
    float3 t_src = zz_cb1_src[3].xyz;
    float3 t_dst = zz_cb1_dst[3].xyz;

    // C = U_dst^-1 × U_src（刚体逆 R^-1 = R^T）：C_rot = Rd × Rs^T，C_t = Rd × (t_src - t_dst)
    float3x3 c_rot = mul(Rd, transpose(Rs));
    float3 c_t = mul(Rd, t_src - t_dst);

    // palette 骨骼：r_i = (Rm 行 i, tm 分量 i)
    float3x3 rm = float3x3(m.r0.xyz, m.r1.xyz, m.r2.xyz);
    float3 tm = float3(m.r0.w, m.r1.w, m.r2.w);

    // M' = C × M：Rm' = C_rot × Rm，tm' = C_rot × tm + C_t
    float3x3 rm_out = mul(c_rot, rm);
    float3 tm_out = mul(c_rot, tm) + c_t;

    ZZBone3x4 out_bone;
    out_bone.r0 = float4(rm_out[0], tm_out.x);
    out_bone.r1 = float4(rm_out[1], tm_out.y);
    out_bone.r2 = float4(rm_out[2], tm_out.z);
    merged_skeleton[slot] = out_bone;
}
