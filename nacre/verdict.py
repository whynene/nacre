拒段 = "拒段"
截尾 = "截尾"
退回 = "退回"
整份 = "整份"

处置们 = (拒段, 截尾, 退回, 整份)

class 段判决:

    __slots__ = ("单元", "过", "问题", "原文", "处置", "载荷", "引用卡号")

    def __init__(self, 单元, 过=True, 问题=(), 原文="", 处置=None, 载荷=None, 引用卡号=()):
        self.单元 = str(单元)
        self.过 = bool(过)
        self.问题 = tuple(问题)
        self.原文 = str(原文 or "")
        self.处置 = 处置
        self.载荷 = 载荷
        self.引用卡号 = tuple(引用卡号)

    def __repr__(self):
        return f"<段判决 {self.单元} {'过' if self.过 else self.处置}>"

    def as_dict(self):
        return {"单元": self.单元, "过": self.过, "问题": list(self.问题),
                "原文": self.原文, "处置": self.处置, "引用卡号": list(self.引用卡号)}

class 判决书(list):

    def __init__(self, 段=(), 整份问题=()):
        self.段 = tuple(段)
        self.整份问题 = tuple(整份问题)
        全部 = list(self.整份问题)
        for s in self.段:
            全部 += list(s.问题)
        super().__init__(全部)

    @property
    def 通过(self):
        return not self

    @property
    def 没救了(self):
        return bool(self.整份问题)

    def 合格(self):
        return [s for s in self.段 if s.过]

    def 不合格(self):
        return [s for s in self.段 if not s.过]

    def 按处置(self, 处置):
        return [s for s in self.段 if not s.过 and s.处置 == 处置]

    @property
    def 要退回(self):
        return bool(self.按处置(退回))

    def as_dict(self):
        return {"整份问题": list(self.整份问题), "段": [s.as_dict() for s in self.段]}

def 段级重试提示词(步骤, 不合格, 卡索引=None, 输出说明=""):
    卡索引 = 卡索引 or {}
    段 = [f"上一发的「{步骤}」产出里，有 **{len(不合格)} 处**没过闸。",
         "🔴 **只重写这几处。别的部分已经落盘了，不要重发、也不要改。**", ""]
    for s in 不合格:
        段.append(f"## `{s.单元}`")
        段.append("**为什么被拒**（每条都写着它违的是哪一条规矩）：")
        段 += [f"- {p}" for p in s.问题]
        段 += ["", "**原文**：", (s.原文 or "（空）"), ""]
    引 = []
    for s in 不合格:
        for n in s.引用卡号:
            if n not in 引:
                引.append(n)
    有的 = [f"#{n} {卡索引[n]}" for n in 引 if n in 卡索引]
    if 有的:
        段 += ["## 这几段引到的卡（只给这几张）", *有的, ""]
    段.append(输出说明)
    return "\n".join(段)
