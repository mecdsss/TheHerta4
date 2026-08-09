// rzm_shapekey_drive.hlsl
// RZMenu / 3DMigoto / XXMI
//
// Drives per-zone shape-key intensity from hover hit + left-click hold.
// Runs once per frame (after rzm_jiggle_screen_state) and integrates a ramp
// into an RWBuffer. The drive is ONLY active in drag system mode 1 ("仅命中",
// hit detection only): while the cursor hits the bound zone AND LMB is held,
// drive[zone] climbs toward 1.0 at rampRate * step; when the click is
// released or the cursor leaves the zone it either holds (releaseDecay == 0)
// or decays by pow(releaseDecay, step) per frame. In mode 0 (off) and mode 2
// (hit + physical drag interaction) the buffer is zeroed so shape keys are
// not driven (the jiggle handles deformation instead).
//
// The shape-key compute shader binds this same buffer as an SRV
// (Buffer<float>) and uses it as the weight for any shape key whose
// drag_zone_id matches the zone. No CPU readback / store/ref involved.
//
// Bindings:
//   t67  = ResourceDragPinnedDetectInfo (hover hit + zone id)
//   u0   = ResourceDragShapeKeyDrive (R32_FLOAT, array = zone capacity)
//   t120 = IniParams
//
// IniParams:
//   [76].x = dt (minutes-based delta), .y = sim speed, .z = max step
//   [77].x = ramp rate per step (default 0.08)
//   [77].y = release decay retention (0 = hold, >0 = per-step decay)
//   [77].z = drag system mode (0=off, 1=hit only, 2=hit + drag)
//   [77].w = LMB held (from $ssmtdrag_lmb_down_<ns>, active in every mode)
//   [78].x = X held (from $ssmtdrag_x_down_<ns>; original design treats X as LMB)

RWBuffer<float> ShapeKeyDrive       : register(u0);
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
    ShapeKeyDrive.GetDimensions(zoneCount);
    if (zoneCount == 0u)
        return;

    float step = SimulationStep();
    float rampRate = SafePositive(DRIVE_PARAMS.x, 0.08);
    float releaseDecay = DRIVE_PARAMS.y;
    float mode = DRIVE_PARAMS.z;
    bool triggerHeld = DRIVE_PARAMS.w > 0.5 || IniParams[78].x > 0.5;

    // 仅在“仅命中”模式（1）下驱动；其余模式（0/2）清零，形态键回到未驱动状态
    if (mode != 1.0)
    {
        for (uint zeroZone = 0u; zeroZone < zoneCount; ++zeroZone)
            ShapeKeyDrive[zeroZone] = 0.0;
        return;
    }

    // 命中 + 左键按下检测：PinnedDetectInfo[0].x >= 0 表示命中，[1].w == 7 表示区域感知命中，
    // [7].w 是区域索引（与 rzm_jiggle_screen_state 的判定一致）
    float4 detected = PinnedDetectInfo[0u];
    bool hasHit = detected.x >= 0.0 && detected.y < 1e30
        && abs(PinnedDetectInfo[1u].w - 7.0) < 0.5;
    hasHit = hasHit && triggerHeld;
    uint hoverZone = ClampZoneID(PinnedDetectInfo[7u].w, zoneCount);

    for (uint zone = 0u; zone < zoneCount; ++zone)
    {
        float current = ShapeKeyDrive[zone];
        float next = current;
        if (hasHit && zone == hoverZone)
        {
            next = min(current + rampRate * step, 1.0);
        }
        else if (releaseDecay > 0.0)
        {
            next = current * pow(saturate(releaseDecay), step);
        }
        ShapeKeyDrive[zone] = next;
    }
}
