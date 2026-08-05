// rzm_ui_pin_readback.hlsl
// Converts the pinned float payload into UI-safe uint readback values.
//
// Expected bindings:
//   cs-t0 = ResourceDragPinnedDetectInfo_<ns>, StructuredBuffer<float4>
//   cs-u0 = ResourceDragUIPinnedDetectID_<ns>, RWStructuredBuffer<uint>
//   cs-u1 = ResourceDragUIPinnedZone_<ns>,    RWStructuredBuffer<uint>
//
// The INI store command reads raw 32-bit data, so float sentinels such as
// -1.0f and 1.0f cannot be compared as numbers. This shader publishes an
// explicit uint sentinel (0xFFFFFFFF) instead.

StructuredBuffer<float4> gPinnedInfo : register(t0);
RWStructuredBuffer<uint> gUIPinnedID : register(u0);
RWStructuredBuffer<uint> gUIPinnedZone : register(u1);

static const uint kInvalid = 0xffffffffu;

[numthreads(1, 1, 1)]
void main(uint3 dispatchThreadID : SV_DispatchThreadID)
{
    float4 slot0 = gPinnedInfo[0u];
    bool invalid = slot0.x < 0.0f || slot0.y > 1e30f;

    uint detectedID = kInvalid;
    uint zoneID = kInvalid;
    if (!invalid)
    {
        detectedID = (uint)max(slot0.x, 0.0f);
        zoneID = (uint)max(round(gPinnedInfo[7u].w), 0.0f);
    }

    gUIPinnedID[0u] = detectedID;
    gUIPinnedZone[0u] = zoneID;
}
