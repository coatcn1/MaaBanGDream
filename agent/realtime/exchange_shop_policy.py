"""交换所只以火罐期望数量为收益，特别礼物仅约束换箱权限。"""
from dataclasses import dataclass
from functools import lru_cache
from math import comb

import numpy as np


# 长期平均收益的继续上界；实际任务使用余额与抽数上限约束的有限预算 DP。
THRESHOLDS = {
    180: (23, 46, 66, 86, 106, 125, 144, 163),
    230: (31, 60, 87, 112, 137, 161, 185, 209),
}
MAX_EXACT_BUDGET = 10000


@dataclass(frozen=True)
class BoxState:
    box: int
    remaining: int
    total: int
    cans: int
    special: int
    currency: int

    def validate(self):
        if any(type(value) is not int for value in self.__dict__.values()):
            raise ValueError("抽箱读数必须是整数")
        if (self.total not in THRESHOLDS or self.box < 1
                or not 0 <= self.remaining <= self.total or not 0 <= self.cans <= 10
                or self.special not in (0, 1) or self.currency < 0
                or self.cans + self.special > self.remaining):
            raise ValueError("抽箱读数范围或剩余物品关系冲突")
        return self


@dataclass(frozen=True)
class Decision:
    action: str
    amount: int = 0
    threshold: int | None = None
    max_steps: int | None = None
    reason: str = ""


class FiniteBudgetPolicy:
    """无放回超几何 Bellman 递推；浮点计算概率，不截断期限或近似长期策略。"""

    def __init__(self, total):
        self.states = [(x,y,s) for x in range(1,total+1)
                       for y in range(min(10,x)+1) for s in (0,1) if y+s <= x]
        self.index = {state:i for i,state in enumerate(self.states)}
        self.full = self.index[(total,10,1)]
        self.amount = np.array([min(10,x) for x,_,_ in self.states])
        self.unlocked = np.array([s == 0 for _,_,s in self.states])
        count = len(self.states)
        self.next = np.zeros((22,count),dtype=np.int32)
        self.probability = np.zeros((22,count),dtype=np.float64)
        self.reward = np.array([min(10,x)*y/x for x,y,_ in self.states])
        for i,(x,y,s) in enumerate(self.states):
            amount=min(10,x)
            denominator=comb(x,amount)
            outcomes=[]
            for got in range(min(y,amount)+1):
                for special in range(s+1):
                    other=amount-got-special
                    if 0 <= other <= x-y-s:
                        following=(x-amount,y-got,s-special)
                        target=self.full if x == amount else self.index[following]
                        probability=comb(y,got)*comb(s,special)*comb(x-y-s,other)/denominator
                        outcomes.append((target,probability))
            for j,(target,probability) in enumerate(outcomes):
                self.next[j,i]=target
                self.probability[j,i]=probability
        self.groups = [(amount,np.flatnonzero(self.amount == amount)) for amount in range(1,11)]
        self.values = [np.zeros(count) for _ in range(11)]
        # 每层只缓存一个字节的动作：0 停止、1 继续、2 换箱；零收益并列时保留货币。
        self.actions = [np.zeros(count,dtype=np.uint8)]
        self.horizon = 0

    def extend(self, budget, stopped):
        while self.horizon < budget:
            stopped()
            horizon=self.horizon+1
            continuation=np.zeros(len(self.states))
            for amount,indices in self.groups:
                if amount <= horizon and indices.size:
                    previous=self.values[(horizon-amount)%11]
                    continuation[indices]=self.reward[indices]+np.sum(
                        self.probability[:,indices]*previous[self.next[:,indices]],axis=0)
            # 新箱特别礼物未获得，不能立即重置；先求它才能消除零费用换箱的同层依赖。
            fresh=continuation[self.full]
            reset=self.unlocked & (fresh > continuation+1e-12)
            values=np.where(reset,fresh,continuation)
            self.values[horizon%11]=values
            self.actions.append(np.where(values > 0,np.where(reset,2,1),0).astype(np.uint8))
            self.horizon=horizon
        stopped()


@lru_cache(maxsize=2)
def finite_policy(total):
    return FiniteBudgetPolicy(total)


def decide(state: BoxState, budget=None, stopped=lambda: None) -> Decision:
    state.validate()
    x,y=state.remaining,state.cans
    if x == 0:
        return Decision("wait",reason="空箱由游戏自动换新箱，只等待送达")
    threshold=THRESHOLDS[state.total][y-1] if 1 <= y <= 8 else None
    if budget is None:
        reset=state.special == 0 and (y == 0 or threshold is not None and x > threshold)
        return Decision("reset" if reset else "draw",0 if reset else min(10,x),threshold,
                        reason="长期火罐期望策略；特别礼物仅限制换箱")
    if type(budget) is not int or budget < 0:
        raise ValueError("有限预算必须是非负整数抽数")
    if budget > MAX_EXACT_BUDGET:
        raise ValueError(f"本次预算过大，请将最大抽数设为{MAX_EXACT_BUDGET}以内")
    policy=finite_policy(state.total)
    policy.extend(budget,stopped)
    action=int(policy.actions[budget][policy.index[(x,y,state.special)]])
    if action == 0:
        return Decision("stop",reason="可用预算内火罐期望为零，保留礼物币")
    if action == 2:
        return Decision("reset",reason=f"有限预算{budget}抽的火罐期望以换新箱更高")
    amount=min(10,x)
    if amount > budget:
        return Decision("stop",reason="剩余预算不足本次十抽或尾数，不发送单抽补足")
    return Decision("draw",amount,reason=f"有限预算{budget}抽的火罐期望以继续抽取最优或并列")


def validate_draw(before: BoxState, after: BoxState, amount: int):
    before.validate()
    after.validate()
    common=(amount == min(10,before.remaining) and amount > 0
            and after.total == before.total and after.currency == before.currency-10*amount)
    automatic=(amount == before.remaining and after.box == before.box+1
               and after.remaining == after.total and after.cans == 10 and after.special == 1)
    ordinary=(amount < before.remaining and after.box == before.box
              and after.remaining == before.remaining-amount
              and after.cans <= before.cans and after.special <= before.special
              and before.cans-after.cans+before.special-after.special <= amount)
    if not common or not (automatic or ordinary):
        raise RuntimeError("抽取结果无法完整对账，禁止重复消费")


def validate_reset(before: BoxState, after: BoxState):
    before.validate()
    after.validate()
    if (before.special != 0 or before.remaining == 0 or after.box != before.box + 1
            or after.total != before.total or after.remaining != after.total
            or after.cans != 10 or after.special != 1 or after.currency != before.currency):
        raise RuntimeError("换箱结果无法确认，禁止重复确认")
