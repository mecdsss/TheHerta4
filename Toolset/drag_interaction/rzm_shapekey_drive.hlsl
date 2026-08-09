// rzm_shapekey_drive.hlsl
// RZMenu / 3DMigoto / XXMI
//
// Drives per-zone shape-key intensity from hover hit + left-click hold.
// Runs once per frame (after rzm_jiggle_screen_state) and writes per-zone
// intensities into an RWBuffer. The drive is ONLY active in drag system
// mode 1 ("仅命中", hit detection only):
//   - Each zone has its own independent segment: 4 directional slots
//     (0=up, 1=right, 2=down, 3=left) followed by N no-direction stage
//     slots (N = ZoneStageCounts[zone], per-zone independent).
//   - Directional slots ignore click stages entirely: while hitting this
//     zone and holding LMB/X, each direction follows that direction's net
//     mouse displacement (opposite direction subtracts), clamped to 0..1.
//   - No-direction stage slots: each press while hovering advances that
//     zone's click count in 0..N cycle (0 = inactive/cleared); the matching
//     stage slot is set to 1 and held until cleared or another stage is
//     active. No mouse displacement is involved.
// Release or leaving the zone holds the current value. In mode 0 (off) and
// mode 2 (hit + physical drag interaction) all buffers are zeroed.
//
// The shape-key compute shader binds the same buffers as SRVs
// (Buffer<float> ShapeKeyDrive, Buffer<uint> ClickCount) and uses them as the
// weight for shape keys bound to (zone, stage, slot). No CPU
// readback.
//
// Bindings:
//   t67  = ResourceDragPinnedDetectInfo (hover hit + zone id)
//   t68  = ResourceDragShapeKeyZoneStageCounts (R32_UINT, array = capacity;
//          per-zone no-direction stage count)
//   u0   = ResourceDragShapeKeyDrive (R32_FLOAT, array = sum(4+N per zone))
//   u1   = ResourceDragShapeKeyDir   (R32_FLOAT, array = sum(4+N per zone)+1;
//          last = prev press state)
//   u2   = ResourceDragShapeKeyClickCount (R32_UINT, array = capacity)
//   u3   = ResourceDragShapeKeyActiveDir (R32_UINT, array = capacity;
//          dominant direction per zone 0=up 1=right 2=down 3=left)
//   t120 = IniParams
//
// IniParams:
//   [77].z = drag system mode (0=off, 1=hit only, 2=hit + drag)
//   [77].w = LMB held (from $ssmtdrag_lmb_down_<ns>, active in every mode)
//   [78].x = X held (from $ssmtdrag_x_down_<ns>; original design treats X as LMB)
//   [79].x = mouse Y displacement (from $ssmtdrag_shapekey_dy_<ns>, px/frame)
//   [79].y = mouse X displacement (from $ssmtdrag_shapekey_dx_<ns>, px/frame)
//   [80].x = mouse displacement sensitivity (per-px strength delta)

RWBuffer<float> ShapeKeyDrive       : register(u0);
RWBuffer<float> ShapeKeyDir         : register(u1);
RWBuffer<uint> ClickCount           : register(u2);
RWBuffer<uint> ActiveDir            : register(u3);
StructuredBuffer<float4> PinnedDetectInfo : register(t67);
Buffer<uint> ZoneStageCounts        : register(t68);
Texture1D<float4> IniParams         : register(t120);

#define DRIVE_PARAMS  IniParams[77]

uint ClampZoneID(float zoneValue, uint zoneCount)
{
    return min((uint)max(round(zoneValue), 0.0), max(zoneCount, 1u) - 1u);
}

[numthreads(1, 1, 1)]
void main(uint3 dispatchThreadID : SV_DispatchThreadID)
{
    uint zoneCount;
    ClickCount.GetDimensions(zoneCount);
    if (zoneCount == 0u)
        return;
    uint driveSlots;
    ShapeKeyDrive.GetDimensions(driveSlots);
    uint dirSlots;
    ShapeKeyDir.GetDimensions(dirSlots);
    // 按区域独立段计算总槽数：每区域 4 方向槽 + 该区域档位数 N 个无方向槽
    uint lastSlot = 0u;
    for (uint z = 0u; z < zoneCount; ++z)
        lastSlot += 4u + max(1u, ZoneStageCounts[z]);
    if (driveSlots < lastSlot || dirSlots < lastSlot + 1u)
        return;

    float mode = DRIVE_PARAMS.z;
    float mouseDy = IniParams[79].x;
    float mouseDx = IniParams[79].y;
    float mouseSensitivity = max(IniParams[80].x, 0.0001);
    bool triggerHeld = DRIVE_PARAMS.w > 0.5 || IniParams[78].x > 0.5;

    bool wasHeld = ShapeKeyDir[lastSlot] > 0.5;
    bool pressed = triggerHeld && !wasHeld;

    // 4 方向相邻混合权重（对齐 UI 构造器 directionCount=4 正交锚点分解）
    // 0=上 1=右 2=下 3=左；位移向量归一化后与锚点点积取正
    float moveLen = length(float2(mouseDx, mouseDy));
    float4 dirWeight = float4(0.0, 0.0, 0.0, 0.0);
    if (moveLen > 1e-4)
    {
        float2 v = float2(mouseDx, mouseDy) / moveLen;
        dirWeight = float4(max(v.y, 0.0), max(v.x, 0.0), max(-v.y, 0.0), max(-v.x, 0.0));
    }
    uint activeDir = 0u;
    float bestW = dirWeight.x;
    for (uint d = 1u; d < 4u; ++d)
    {
        if (dirWeight[d] > bestW) { bestW = dirWeight[d]; activeDir = d; }
    }

    // 仅在“仅命中”模式（1）下驱动；其余模式（0/2）清零
    if (mode != 1.0)
    {
        for (uint zeroIdx = 0u; zeroIdx < driveSlots; ++zeroIdx)
        {
            ShapeKeyDrive[zeroIdx] = 0.0;
            ShapeKeyDir[zeroIdx] = 0.0;
        }
        for (uint zeroZone = 0u; zeroZone < zoneCount; ++zeroZone)
        {
            ClickCount[zeroZone] = 0u;
            ActiveDir[zeroZone] = 0u;
        }
        ShapeKeyDir[lastSlot] = triggerHeld ? 1.0 : 0.0;
        return;
    }

    // 命中 + 左键/X 按下：PinnedDetectInfo[0].x >= 0 表示命中，[1].w == 7 表示区域感知命中，
    // [7].w 是区域索引（与 rzm_jiggle_screen_state 的判定一致）
    float4 detected = PinnedDetectInfo[0u];
    bool hasHit = detected.x >= 0.0 && detected.y < 1e30
        && abs(PinnedDetectInfo[1u].w - 7.0) < 0.5;
    hasHit = hasHit && triggerHeld;
    uint hoverZone = ClampZoneID(PinnedDetectInfo[7u].w, zoneCount);

    // 按下瞬间：推进该区域点击档位（0..zoneStageCount 循环；0=未激活/清空）
    if (pressed && hasHit)
    {
        uint oldStage = ClickCount[hoverZone];
        uint zoneStageCount = max(1u, ZoneStageCounts[hoverZone]);
        uint newStage = oldStage >= zoneStageCount ? 0u : oldStage + 1u;
        ClickCount[hoverZone] = newStage;
    }
    ShapeKeyDir[lastSlot] = triggerHeld ? 1.0 : 0.0;

    uint runningBase = 0u;
    for (uint zone = 0u; zone < zoneCount; ++zone)
    {
        uint zoneStageCount = max(1u, ZoneStageCounts[zone]);
        uint zoneBase = runningBase;
        runningBase += 4u + zoneStageCount;
        bool zoneHit = hasHit && zone == hoverZone;
        bool zonePressed = zoneHit && pressed;
        uint activeStage = ClickCount[zone];
        // 方向槽：忽略档位，按住命中该区域时由鼠标位移驱动；否则保持
        for (uint dir = 0u; dir < 4u; ++dir)
        {
            uint idx = zoneBase + dir;
            float current = ShapeKeyDrive[idx];
            float next = current;
            if (zoneHit)
            {
                // 位移驱动：该方向净位移（同向 +、对向 -）× 灵敏度，
                // 向上时“上”增“下”减，向下时反之，左右同理
                float net = dirWeight[dir] - dirWeight[(dir + 2u) % 4u];
                next = clamp(current + net * moveLen * mouseSensitivity, 0.0, 1.0);
            }
            ShapeKeyDrive[idx] = next;
        }
        // 无方向档位槽：仅当前命中区域按下该档位时置 1 并保持；非活动/清空时归 0。
        // 必须同时满足 zoneHit，否则在其他区域按下时会误置本区域槽
        for (uint stage = 1u; stage <= zoneStageCount; ++stage)
        {
            uint ndIdx = zoneBase + 4u + (stage - 1u);
            if (activeStage == stage && zonePressed)
                ShapeKeyDrive[ndIdx] = 1.0;
            else if (activeStage != stage)
                ShapeKeyDrive[ndIdx] = 0.0;
        }
        if (zoneHit)
            ActiveDir[zone] = activeDir;
    }
}
