// rzm_shapekey_var_sync.hlsl
// RZMenu / 3DMigoto / XXMI
//
// Syncs shape-key export variables into the ShapeKeyDrive buffer.
// Runs once per frame (unconditional, independent of drag system mode) so
// the variable-driven path and the drag-driven path share one source of
// truth.
//
// Background: shape keys bound to a drag zone read their weight from
// ShapeKeyDrive[slot] only (see rzm_shapekey_drive.hlsl and the anim CS
// FREQ defines); their export variable ($Freq_*) never reaches the shader.
// Without this pass, changing the variable leaves the buffer stale and the
// next drag resumes from the stale value (visible snap back to 0).
//
// Semantics: the two writers of ShapeKeyDrive are mutually exclusive in
// time, gated by the per-zone ZoneActive flag recomputed HERE every frame
// (same hit test as rzm_shapekey_drive: hit-only mode + grab modifier +
// target drawn + LMB/X held + zone hit):
//   - Zone inactive: the variable owns the buffer. The buffer is written
//     only when the variable actually changed this frame (directional:
//     slot = v; no-direction stage: slot = v plus click-count open/clear).
//   - Zone active (dragging): the drive CS owns the buffer; the
//     variable->buffer write is SUSPENDED so an animation driver stepping
//     the variable mid-drag cannot yank the buffer away from the drag.
// After the drag ends (flag falls) the buffer keeps the dragged value:
// VarSyncPrev tracked the variable silently during the suspension, so no
// write fires until the variable actually changes again.
//
// GPU->CPU synchronization is emitted by the generated
// CommandListDragShapeKeyVarReadback section. It reads ZoneActive and the
// drive slot with the loader's direct-resource store syntax (without ref),
// then mirrors the dragged value into the export variable. A short cooldown
// after release absorbs the asynchronous store latency before ownership is
// returned to the variable-driven path.
//
// Bindings:
//   t67  = ResourceDragPinnedDetectInfo (hover hit + zone id; same SRV the
//          drive CS uses for its hit test)
//   t69  = ResourceDragShapeKeyVarSyncMap (R32G32B32A32_UINT, one uint4 per
//          binding: x = drive slot, y = zone id, z = nd_stage or 0xFFFFFFFF,
//          w = reserved; baked at export)
//   u0   = ResourceDragShapeKeyDrive (shared with rzm_shapekey_drive)
//   u1   = ResourceDragShapeKeyClickCount (shared with rzm_shapekey_drive)
//   u2   = ResourceDragShapeKeyVarPrev (R32_FLOAT, array = binding count;
//          previous variable values, boot-cleared to 0)
//   u3   = ResourceDragShapeKeyClickCountF (R32_FLOAT, array = zone capacity;
//          float mirror kept coherent with ClickCount for CPU store export)
//   u4   = ResourceDragShapeKeyZoneActive (R32_FLOAT, array = zone capacity;
//          per-zone drag-active flags, recomputed every frame below)
//   t120 = IniParams
//
// IniParams (packed 4 variables per float4, from index 81; 76-80 are used
// by the drive CS, 100+ by the shape-key anim CS):
//   [81 + i/4][i%4] = current value of binding i's export variable
// IniParams[75] gate inputs (fixed below the drive CS range):
//   x = drag system mode (1 = hit only), y = grab modifier + LMB/X held,
//   z = target drawn this frame, w = input mode (0 = game)

RWBuffer<float> ShapeKeyDrive       : register(u0);
RWBuffer<uint>  ClickCount          : register(u1);
RWBuffer<float> VarSyncPrev         : register(u2);
RWBuffer<float> ClickCountF         : register(u3);
RWBuffer<float> ZoneActive          : register(u4);
Buffer<uint4>   VarSyncMap          : register(t69);
StructuredBuffer<float4> PinnedDetectInfo : register(t67);
Texture1D<float4> IniParams         : register(t120);

#define VAR_SYNC_INIPARAM_BASE 81
#define VAR_SYNC_GATE_PARAMS 75

uint ClampZoneID(float zoneValue, uint zoneCount)
{
    return min((uint)max(round(zoneValue), 0.0), max(zoneCount, 1u) - 1u);
}

[numthreads(1, 1, 1)]
void main(uint3 dispatchThreadID : SV_DispatchThreadID)
{
    uint count;
    VarSyncMap.GetDimensions(count);
    if (count == 0u)
        return;
    uint driveSlots;
    ShapeKeyDrive.GetDimensions(driveSlots);
    uint clickSlots;
    ClickCount.GetDimensions(clickSlots);
    uint clickFloatSlots;
    ClickCountF.GetDimensions(clickFloatSlots);
    uint prevSlots;
    VarSyncPrev.GetDimensions(prevSlots);
    if (clickSlots == 0u || clickFloatSlots < clickSlots || prevSlots < count)
        return;

    // 拖拽激活标志（每区域，与驱动 CS 同一命中判定）：
    // 仅命中模式 + 目标绘制 + LMB/X 按住 + 命中区域 → 该区域的缓冲归拖拽 CS 所有。
    // 本 CS 每帧无条件重算（覆盖驱动 CS 不运行的帧：松开 ALT/切换模式/目标移出），
    // 保证标志必然回落。
    float4 gate = IniParams[VAR_SYNC_GATE_PARAMS];
    bool gateOn = gate.x == 1.0 && gate.y > 0.5 && gate.z > 0.5 && gate.w < 0.5;
    float4 detected = PinnedDetectInfo[0u];
    bool hasHit = gateOn && detected.x >= 0.0 && detected.y < 1e30
        && abs(PinnedDetectInfo[1u].w - 7.0) < 0.5;
    uint hoverZone = ClampZoneID(PinnedDetectInfo[7u].w, clickSlots);
    for (uint z = 0u; z < clickSlots; ++z)
        ZoneActive[z] = (hasHit && z == hoverZone) ? 1.0 : 0.0;

    for (uint i = 0u; i < count; ++i)
    {
        float raw = IniParams[VAR_SYNC_INIPARAM_BASE + (i >> 2)][i & 3];
        if (abs(raw - VarSyncPrev[i]) <= 1e-6)
            continue;
        // 跟踪变量值：拖拽激活期间只跟踪不写入，松手后缓冲保持拖拽值
        VarSyncPrev[i] = raw;
        uint4 mapping = VarSyncMap[i];
        uint slot = mapping.x;
        uint zone = mapping.y;
        uint ndStage = mapping.z;
        // 该区域拖拽激活中：缓冲归拖拽 CS 所有，变量→缓冲写入挂起，
        // 避免驱动器中途步进把缓冲从拖拽值上拽走（双向打架）
        if (zone < clickSlots && ZoneActive[zone] > 0.5)
            continue;
        float v = clamp(raw, 0.0, 1.0);
        if (slot < driveSlots)
            ShapeKeyDrive[slot] = v;
        if (ndStage != 0xFFFFFFFFu && zone < clickSlots)
        {
            // 无方向档位的读取带 ClickCount 门控：变量非 0 时打开对应档位，
            // 归 0 时仅在当前活动档位就是本档位才清空（不干扰点击切出的其他档位）
            if (v > 1e-6)
                ClickCount[zone] = ndStage;
            else if (ClickCount[zone] == ndStage)
                ClickCount[zone] = 0u;
            ClickCountF[zone] = (float)ClickCount[zone];
        }
    }
}
