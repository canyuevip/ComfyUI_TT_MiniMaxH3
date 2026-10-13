# -*- coding: utf-8 -*-
"""编组开关（按数量）节点。

在「忽略多组」的编组开关面板基础上增加一个 INT 输入端：按数量控制工作流中各
编组的开启/忽略状态。

    0 = 全部关闭
    1 = 只开启第 1 个开关
    2 = 只开启前 2 个开关
    N = 只开启前 N 个开关

节点本身不参与运算，仅作为前端开关面板的宿主（前端按编组顺序应用前 N 个）。
"""


class TTGroupSwitch:
    """编组开关：INT 控制开启列表中的前 N 个编组。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "开启数量": ("INT", {
                    "default": 0,
                    "min": 0,
                    "max": 64,
                    "step": 1,
                    "tooltip": "0=全部关闭；N=开启开关列表中的前 N 个编组，其余忽略",
                }),
            },
        }

    RETURN_TYPES = ()
    FUNCTION = "execute"
    CATEGORY = "TT_MiniMaxH3/工具"
    OUTPUT_NODE = True

    def execute(self, 开启数量=0):
        return ()
