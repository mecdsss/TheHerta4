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
// Semantics (value arbitration, variable-first):
//   - The variable changed this frame (hotkey / animation driver) -> the
//     variable owns the buffer and is written immediately, even while a
//     drag is active (variable always wins).
//   - CPU/GPU arbitration uses a mode handshake in IniParams[90..98]:
//     mode 1 suppresses a CPU readback echo; mode 2 force-pushes a changed
//     variable until the delayed store readback confirms the same value.
//   - ZoneActive is still recomputed here every frame (same hit test as
//     rzm_shapekey_drive) and consumed by the CPU readback to decide which
//     side owns the variable.
// GPU->CPU synchronization is emitted by the generated
// CommandListDragShapeKeyVarReadback section. It reads ZoneActive and the
// drive slot with the loader's direct-resource store syntax (without ref).
// The pending/ack settle handshake absorbs store latency before pulls resume,
// so a fresh hotkey value is never clobbered by a stale buffer read.
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
//   [83 + i/4][i%4] = CPU readback pull flag of binding i (1 = the value
//                     change this frame came from the buffer pull; suppress
//                     the variable->buffer push this frame to avoid echo)
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
#define VAR_SYNC_MODE_BASE 90
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
        float syncMode = IniParams[VAR_SYNC_MODE_BASE + (i >> 2)][i & 3];
        bool forcePush = syncMode > 1.5;
        if (!forcePush)
        {
            if (abs(raw - VarSyncPrev[i]) <= 1e-6)
                continue;
        }
        VarSyncPrev[i] = raw;
        // mode=1 表示 CPU 刚从缓冲拉取，跳过回声；mode=2 表示变量值仍
        // 等待 store 确认，必须绕过 prev 去重持续推送，直到 CPU 观察到追平。
        if (syncMode > 0.5 && syncMode < 1.5)
            continue;
        uint4 mapping = VarSyncMap[i];
        uint slot = mapping.x;
        uint zone = mapping.y;
        uint ndStage = mapping.z;
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
