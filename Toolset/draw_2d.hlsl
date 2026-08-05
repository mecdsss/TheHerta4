// **** RESPONSIVE UI SHADER (Dynamic Sampling - 3dmigoto Compatible) ****
// Contributors: SinsOfSeven, Fixed by Assistant
// 修复说明：增加了点采样器，根据缩放比例动态切换采样方式，彻底解决放大模糊和缩小锯齿问题。

Texture1D<float4> IniParams : register(t120);

#define SIZE IniParams[87].xy
#define OFFSET IniParams[87].zw

struct vs2ps {
	float4 pos : SV_Position0;
	float2 uv : TEXCOORD1;
};

#ifdef VERTEX_SHADER
void main(
		out vs2ps output,
		uint vertex : SV_VertexID)
{
	float2 BaseCoord,Offset;
	Offset.x = OFFSET.x*2-1;
	Offset.y = (1-OFFSET.y)*2-1;
	BaseCoord.xy = float2((2*SIZE.x),(2*(-SIZE.y)));
	switch(vertex) {
		case 0:
			output.pos.xy = float2(BaseCoord.x+Offset.x, BaseCoord.y+Offset.y);
			output.uv = float2(1,0);
			break;
		case 1:
			output.pos.xy = float2(BaseCoord.x+Offset.x, 0+Offset.y);
			output.uv = float2(1,1);
			break;
		case 2:
			output.pos.xy = float2(0+Offset.x, BaseCoord.y+Offset.y);
			output.uv = float2(0,0);
			break;
		case 3:
			output.pos.xy = float2(0+Offset.x, 0+Offset.y);
			output.uv = float2(0,1);
			break;
		default:
			output.pos.xy = 0;
			output.uv = float2(0,0);
			break;
	};
	output.pos.zw = float2(0, 1);
}
#endif

#ifdef PIXEL_SHADER
Texture2D<float4> tex : register(t100);

// 线性采样器（用于缩小/抗锯齿）
sampler linearSampler : register(s0) = sampler_state
{
    Filter = MIN_MAG_MIP_LINEAR;
    AddressU = Clamp;
    AddressV = Clamp;
};

// 点采样器（用于放大时保持清晰）
sampler pointSampler : register(s1) = sampler_state
{
    Filter = MIN_MAG_MIP_POINT;
    AddressU = Clamp;
    AddressV = Clamp;
};

void main(vs2ps input, out float4 result : SV_Target0)
{
    float2 dims;
    tex.GetDimensions(dims.x, dims.y);
    if (!dims.x || !dims.y) discard;

    input.uv.y = 1 - input.uv.y;

    // 计算当前纹理在屏幕上的实际缩放比例
    // 顶点四边形尺寸为 (2*SIZE.x, 2*SIZE.y)
    float2 scale = float2(2 * SIZE.x, 2 * SIZE.y) / dims;
    float maxScale = max(scale.x, scale.y);

    // 动态采样：放大时用点采样（锐利），缩小时用线性采样（抗锯齿）
    if (maxScale > 1.0f) {
        result = tex.Sample(pointSampler, input.uv);
    } else {
        result = tex.Sample(linearSampler, input.uv);
    }
}
#endif