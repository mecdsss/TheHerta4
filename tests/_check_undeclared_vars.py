import os
import re

def analyze(path, tag):
    if not os.path.isfile(path):
        print(f'=== {tag} ===')
        print(f'  跳过：文件不存在 ({path})')
        return []
    text = open(path, encoding='utf-8', errors='replace').read()
    declared = set(re.findall(r'^\s*global(?:\s+persist)?\s+(\$[\w.]+)', text, re.M))
    used = set(re.findall(r'(\$[A-Za-z_][\w.]*)', text))
    locals_ = set(re.findall(r'^\s*local\s+(\$[\w.]+)', text, re.M))
    # 3DMigoto 内建变量（无需声明）
    builtins = {'$active', '$swapkey', '$frame', '$object', '$draw', '$ShaderModel',
                '$windows', '$normal', '$comp', '$lambda', '$texture_count'}
    undeclared = sorted(v for v in used - declared - locals_ - builtins)
    print(f'=== {tag} ===')
    print(f'  global声明: {len(declared)}, 使用: {len(used)}, local: {len(locals_)}')
    print(f'  用到但未声明: {len(undeclared)}')
    for v in undeclared[:40]:
        print('   ', v)
    return undeclared

a = analyze(r'K:/SSMT-Package-master/3Dmigoto/ZZZ/Mods/SSMTGeneratedMod/蕾米埃尔/蕾米埃尔.ini', '我的导出 ini')
b = analyze(r'C:/Users/anlingQWQ/Desktop/[LL] Remielle White - LEWDHAND Ver/Remielle_MT.ini', '原作 ini')
ssmt = [v for v in a if 'ssmtdrag' in v or 'Drag' in v or 'drag' in v]
print()
print('我的 ini 中未声明的拖拽变量:', ssmt if ssmt else '(无)')
