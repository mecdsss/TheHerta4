// rzm_shapekey_drive.hlsl
// RZMenu / 3DMigoto / XXMI
//
// Drives per-zone shape-key intensity from hover hit + left-click hold.
// Runs once per frame (after rzm_jiggle_screen_state) and writes per-zone /
// per-click-stage intensities into an RWBuffer. The drive is ONLY active in
// drag system mode 1 ("仅命中", hit detection only):
//   - Click stages: each press while hovering the bound zone advances that
//     zone's click count in 0..stageCount cycle (0 = inactive/cleared),
//     enabling a different shape-key group per click count and clearing on
//     the wrap-around press ("multi-stage switch").
//   - Directional slots (0=up, 1=right, 2=down, 3=left): while the stage is
//     active, each direction's intensity follows that direction's net mouse
//     displacement (opposite direction subtracts): move up -> up grows and
//     down shrinks, move down -> down grows and up shrinks, etc. Clamped to
//     0..1, no ramp/decay.
//   - No-direction slot (4): while the stage is active, clicking sets the
//     intensity to 1 (held until the zone is cleared or another stage is
//     active). No mouse displacement is involved.
// Release or leaving the zone holds the current value. In mode 0 (off) and
// mode 2 (hit + physical drag interaction) all buffers are zeroed.
//
// The shape-key compute shader binds the same buffers as SRVs
// (Buffer<float> ShapeKeyDrive, Buffer<uint> ClickCount) and uses them as the
// weight for shape keys bound to (zone, click stage, slot). No CPU
// readback.
//
// Bindings:
//   t67  = ResourceDragPinnedDetectInfo (hover hit + zone id)
//   u0   = ResourceDragShapeKeyDrive (R32_FLOAT, array = capacity*stage*5)
//   u1   = ResourceDragShapeKeyDir   (R32_FLOAT, array = capacity*stage*5+1;
//          last = prev press state)
//   u2   = ResourceDragShapeKeyClickCount (R32_UINT, array = capacity)
//   u3   = ResourceDragShapeKeyActiveDir (R32_UINT, array = capacity;
//          dominant direction per zone 0=up 1=right 2=down 3=left)
//   t120 = IniParams
//
// IniParams:
//   [76].x = dt (minutes-based delta), .y = sim speed, .z = max step
//   [77].x = ramp rate per step (default 0.08)
//   [77].y = release decay retention (0 = hold, >0 = per-step decay)
//   [77].z = drag system mode (0=off, 1=hit only, 2=hit + drag)
//   [77].w = LMB held (from $ssmtdrag_lmb_down_<ns>, active in every mode)
//   [78].x = X held (from $ssmtdrag_x_down_<ns>; original design treats X as LMB)
//   [79].x = mouse Y displacement (from $ssmtdrag_shapekey_dy_<ns>, px/frame)
//   [79].y = mouse X displacement (from $ssmtdrag_shapekey_dx_<ns>, px/frame)
//   [79].z = click stage count (1 = single stage, backward compatible)
//   [80].x = mouse displacement sensitivity (per-px strength delta)

RWBuffer<float> ShapeKeyDrive       : register(u0);
RWBuffer<float> ShapeKeyDir         : register(u1);
RWBuffer<uint> ClickCount           : register(u2);
RWBuffer<uint> ActiveDir            : register(u3);
StructuredBuffer<float4> PinnedDetectInfo : register(t67);
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
    uint stageCount = max(1u, (uint)round(IniParams[79].z));
    uint slotCount = 5u; // 0-3 = 上/右/下/左方向，4 = 无方向
    uint perZone = stageCount * slotCount;
    uint lastSlot = zoneCount * perZone;
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

    // 按下瞬间：推进该区域点击档位（0..stageCount 循环；0=未激活/清空）
    if (pressed && hasHit)
    {
        uint oldStage = ClickCount[hoverZone];
        uint newStage = oldStage >= stageCount ? 0u : oldStage + 1u;
        ClickCount[hoverZone] = newStage;
    }
    ShapeKeyDir[lastSlot] = triggerHeld ? 1.0 : 0.0;

    for (uint zone = 0u; zone < zoneCount; ++zone)
    {
        uint activeStage = ClickCount[zone];
        for (uint stage = 1u; stage <= stageCount; ++stage)
        {
            uint stageBase = zone * perZone + (stage - 1u) * slotCount;
            // 档位激活只看点击计数，与是否按住/命中无关（点击后松手保持当前档位）
            bool stageActive = (activeStage == stage);
            // 无方向槽：命中按下该档位时置 1 并保持；非活动/清空时归 0
            uint ndIdx = stageBase + 4u;
            if (stageActive && pressed)
                ShapeKeyDrive[ndIdx] = 1.0;
            else if (!stageActive)
                ShapeKeyDrive[ndIdx] = 0.0;
            for (uint dir = 0u; dir < 4u; ++dir)
            {
                uint idx = stageBase + dir;
                float current = ShapeKeyDrive[idx];
                float next = current;
                if (stageActive)
                {
                    // 位移驱动：该方向净位移（同向 +、对向 -）× 灵敏度，
                    // 向上时“上”增“下”减，向下时反之，左右同理
                    float net = dirWeight[dir] - dirWeight[(dir + 2u) % 4u];
                    if (hasHit && zone == hoverZone)
                        next = clamp(current + net * moveLen * mouseSensitivity, 0.0, 1.0);
                    // 松手/离开时保持当前强度（不归零、不积分）
                }
                else
                {
                    // 非活动档位：切档/清空后归 0，避免上一档残留
                    next = 0.0;
                }
                ShapeKeyDrive[idx] = next;
            }
        }
        if (hasHit && zone == hoverZone)
            ActiveDir[zone] = activeDir;
    }
}
