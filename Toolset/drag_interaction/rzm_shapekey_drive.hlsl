// rzm_shapekey_drive.hlsl
// RZMenu / 3DMigoto / XXMI
//
// Drives per-zone shape-key intensity from the shared drag interaction state.
// Runs once per frame (after rzm_jiggle_screen_state) and integrates a ramp
// into an RWBuffer: while a zone is locked by a real grab, drive[zone]
// climbs toward 1.0 at rampRate * step; on release it either holds
// (releaseDecay == 0) or decays by pow(releaseDecay, step) per frame.
//
// The shape-key compute shader binds this same buffer as an SRV
// (Buffer<float>) and uses it as the weight for any shape key whose
// drag_zone_id matches the zone. No CPU readback / store/ref involved.
//
// Bindings:
//   t71  = ResourceDragJiggleScreenState (shared interaction state)
//   u0   = ResourceDragShapeKeyDrive (R32_FLOAT, array = zone capacity)
//   t120 = IniParams
//
// IniParams:
//   [76].x = dt (minutes-based delta), .y = sim speed, .z = max step
//   [77].x = ramp rate per step (default 0.08)
//   [77].y = release decay retention (0 = hold, >0 = per-step decay)

RWBuffer<float> ShapeKeyDrive       : register(u0);
Buffer<float4> JiggleScreenState    : register(t71);
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

    // InteractionState[9].x = committed active zone; slot 3.w = 1.0 when a
    // real grab is locked (0.5 = charging, 0.0 = idle).
    float4 zoneState = JiggleScreenState[9u];
    float locked = JiggleScreenState[3u].w;
    uint zone = ClampZoneID(zoneState.x, zoneCount);

    float step = SimulationStep();
    float rampRate = SafePositive(DRIVE_PARAMS.x, 0.08);
    float releaseDecay = DRIVE_PARAMS.y;

    float current = ShapeKeyDrive[zone];
    float next;
    if (locked > 0.5)
    {
        next = min(current + rampRate * step, 1.0);
    }
    else if (releaseDecay > 0.0)
    {
        next = current * pow(saturate(releaseDecay), step);
    }
    else
    {
        next = current;
    }

    ShapeKeyDrive[zone] = next;
}
