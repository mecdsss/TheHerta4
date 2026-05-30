from dataclasses import dataclass, field


@dataclass
class D3D11Element:
    """D3D11 顶点元素数据类，描述一个顶点语义元素（如 POSITION、NORMAL、TEXCOORD 等）的格式和布局"""
    SemanticName:str
    SemanticIndex:int
    Format:str
    ByteWidth:int
    # Which type of slot and slot number it use? eg:vb0
    ExtractSlot:str
    # Is it from pointlist or trianglelist or compute shader?
    ExtractTechnique:str
    # Human named category, also will be the buf file name suffix.
    Category:str

    # Fixed items
    InputSlot:str = field(default="0", init=False, repr=False)
    InputSlotClass:str = field(default="per-vertex", init=False, repr=False)
    InstanceDataStepRate:str = field(default="0", init=False, repr=False)

    # Generated Items
    ElementNumber:int = field(init=False,default=0)
    AlignedByteOffset:int
    ElementName:str = field(init=False,default="")

    def __post_init__(self):
        self.ElementName = self.get_indexed_semantic_name()

    def get_indexed_semantic_name(self)->str:
        """获取带索引的语义名称，如 TEXCOORD1、COLOR0 等"""
        if self.SemanticIndex == 0:
            return self.SemanticName
        else:
            return self.SemanticName + str(self.SemanticIndex)
        