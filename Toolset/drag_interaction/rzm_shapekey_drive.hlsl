// rzm_shapekey_drive.hlsl
// RZMenu / 3DMigoto / XXMI
//
// Drives per-zone shape-key intensity from hover hit + left-click hold.
// Runs once per frame (after rzm_jiggle_screen_state) and writes per-zone /
// per-click-stage intensities into an RWBuffer. The drive is ONLY active in
// drag system mode 1 ("仅命中", hit detection only):
//   - Click stages: each press while hovering the bound zone advances that
//     zone's click count (1..stageCount, wrapping), enabling a different
//     shape-key group per click count ("multi-stage switch").
//   - RAMP input: the active stage integrates at rampRate * step toward 1.0,
//     or descends back to 0 when the current intensity was above 0.5 at press.
//   - MOUSE input: each direction's intensity follows that direction's net
//     displacement (opposite direction subtracts): move up -> up grows and
//     down shrinks, move down -> down grows and up shrinks, etc. Clamped to
//     0..1, no ramp/decay.
// Release or leaving the zone holds the current value (releaseDecay == 0) or
// decays it by pow(releaseDecay, step) per frame (RAMP input only). In mode 0
// (off) and mode 2 (hit + physical drag interaction) all buffers are zeroed.
//
// The shape-key compute shader binds the same buffers as SRVs
// (Buffer<float> ShapeKeyDrive, Buffer<uint> ClickCount) and uses them as the
// weight for shape keys bound to (zone, click stage, direction). No CPU
// readback.
//
// Bindings:
//   t67  = ResourceDragPinnedDetectInfo (hover hit + zone id)
//   u0   = ResourceDragShapeKeyDrive (R32_FLOAT, array = capacity*stage*4)
//   u1   = ResourceDragShapeKeyDir   (R32_FLOAT, array = capacity*stage*4+1;
//          per-slot ramp direction 0=up / 1=down; last = prev press state)
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
//   [79].w = input mode (0=RAMP, 1=MOUSE displacement)
//   [80].x = mouse displacement sensitivity (per-px strength delta)

RWBuffer<float> ShapeKeyDrive       : register(u0);
RWBuffer<float> ShapeKeyDir         : register(u1);
RWBuffer<uint> ClickCount           : register(u2);
RWBuffer<uint> ActiveDir            : register(u3);
StructuredBuffer<float4> PinnedDetectInfo : register(t67);
Texture1D<float4> IniParams         : register(t120);

#define TIME_PARAMS   IniParams[76]
#define DRIVE_PARAMS  IniParams[77]

float SafePositive(float value, float fallback)
{
    return value > 0.0 ? value : fallback;
}

float SimulationStep()
{
    float dt = SafePositive(TIME_PARAMS.x, 1.0 / 60.0);
    float speed = SafePositive(TIME_PARAMS.y, 1.0);
    float maxStep = SafePositive(TIME_PARAMS.z, 2.0);
    return clamp(dt * 60.0 * speed, 0.05, maxStep);
}

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
    uint dirCount = 4u; // 上/右/下/左
    uint perZone = stageCount * dirCount;
    uint lastSlot = zoneCount * perZone;
    if (driveSlots < lastSlot || dirSlots < lastSlot + 1u)
        return;

    float step = SimulationStep();
    float rampRate = SafePositive(DRIVE_PARAMS.x, 0.08);
    float releaseDecay = DRIVE_PARAMS.y;
    float mode = DRIVE_PARAMS.z;
    float mouseDy = IniParams[79].x;
    float mouseDx = IniParams[79].y;
    float mouseSensitivity = SafePositive(IniParams[80].x, 0.02);
    bool mouseInput = IniParams[79].w > 0.5;
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
    for (uint d = 1u; d < dirCount; ++d)
    {
        if (dirWeight[d] > bestW) { bestW = dirWeight[d]; activeDir = d; }
    }

    // 仅在“仅命中”模式（1）下驱动；其余模式（0/2）清零
    if (mode != 1.0)
    {
        for (uint zeroIdx = 0u; zeroIdx < driveSlots; ++zeroIdx)
        {
            ShapeKeyDrive[zeroIdx] = 0.0;
            ShapeKeyDir[zeroIdx] = 0.0; // 0 = 上升
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

    // 按下瞬间：推进该区域点击档位（1..stageCount 循环）；新档位在 RAMP 模式下
    // 按当前强度锁定方向（<=0.5 上升，>0.5 下降回 0）
    if (pressed && hasHit)
    {
        uint oldStage = ClickCount[hoverZone];
        uint newStage = (oldStage % stageCount) + 1u;
        ClickCount[hoverZone] = newStage;
        // RAMP 模式：按下时按当前强度为各方向锁定升降方向
        uint stageBase = hoverZone * perZone + (newStage - 1u) * dirCount;
        for (uint d = 0u; d < dirCount; ++d)
        {
            float current = ShapeKeyDrive[stageBase + d];
            ShapeKeyDir[stageBase + d] = current <= 0.5 ? 0.0 : 1.0;
        }
    }
    ShapeKeyDir[lastSlot] = triggerHeld ? 1.0 : 0.0;

    for (uint zone = 0u; zone < zoneCount; ++zone)
    {
        uint activeStage = ClickCount[zone];
        for (uint stage = 1u; stage <= stageCount; ++stage)
        {
            uint stageBase = zone * perZone + (stage - 1u) * dirCount;
            bool stageActive = (activeStage == stage) && hasHit && zone == hoverZone;
            for (uint dir = 0u; dir < dirCount; ++dir)
            {
                uint idx = stageBase + dir;
                float current = ShapeKeyDrive[idx];
                float next = current;
                if (stageActive)
                {
                    if (mouseInput)
                    {
                        // 位移驱动：该方向净位移（同向 +、对向 -）× 灵敏度，
                        // 向上时“上”增“下”减，向下时反之，左右同理
                        float net = dirWeight[dir] - dirWeight[(dir + 2u) % 4u];
                        next = clamp(current + net * moveLen * mouseSensitivity, 0.0, 1.0);
                    }
                    else if (ShapeKeyDir[idx] > 0.5)
                    {
                        next = max(current - rampRate * step, 0.0); // 下降回 0
                    }
                    else
                    {
                        next = min(current + rampRate * step, 1.0); // 上升至 1
                    }
                }
                else if (releaseDecay > 0.0 && !mouseInput)
                {
                    next = current * pow(saturate(releaseDecay), step);
                }
                ShapeKeyDrive[idx] = next;
            }
        }
        if (hasHit && zone == hoverZone)
            ActiveDir[zone] = activeDir;
    }
}
