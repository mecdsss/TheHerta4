
class M_Key:
    """按键配置类，用于存储按键相关的配置信息，包括按键名称、值、备注等"""

    def __init__(self):
        self.key_name = ""  # 声明的 key 名称，格式为 $swapkeyN
        self.key_value = ""  # 具体的按键 VK 值
        self.value_list:list[int] = []  # 值列表，用于循环切换
        
        self.initialize_value = 0  # 初始值
        self.initialize_vk_str = ""  # 虚拟按键组合，遵循 3Dmigoto 的解析格式

        self.tmp_value = 0  # 用于 chain_key_list 中传递使用的临时值
        
        self.comment = ""  # 备注信息，会以注释形式生成到配置表中
        
        self.condition_operator = "&&"  # 该条件与前面条件之间的逻辑运算符（第一个条件忽略此值）
        self.is_swapkey = False  # 标记是否为物体切换节点的变量（由 node_swap_ini.py 单独处理）

    def __str__(self) -> str:
        return (f"M_Key(key_name='{self.key_name}', key_value='{self.key_value}', "
                f"value_list={self.value_list}, initialize_value={self.initialize_value}, "
                f"tmp_value={self.tmp_value}, comment='{self.comment}', "
                f"condition_operator='{self.condition_operator}')")
    
