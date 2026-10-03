# -*- coding: utf-8 -*-
"""自爆询问策略回归测试（针对真实对局里发现的缺陷）。

覆盖：
  * 同一天内每只狼只被问一次自爆（不再每有人发言就重问一遍）
  * 狼人先判定自爆、再发言（避免"先发言后自爆"的时序错乱）
  * 自爆台词只输出一条（不再公开层 + 狼队层各来一遍）
  * 拒绝自爆后当天不再被追问
  * 新的一天会重新获得一次判定机会
"""
import json
import sys
import os

sys.path.insert(0, r"C:\Users\Administrator\Desktop\AI狼人杀")
os.chdir(r"C:\Users\Administrator\Desktop\AI狼人杀")

import game_engine as G
from game_engine import (ROLE_GUARD, ROLE_HUNTER, ROLE_SEER, ROLE_VILLAGER,
                         ROLE_WHITE_WOLF_KING, ROLE_WITCH, ROLE_WOLF,
                         ROLE_WOLF_KING, WerewolfGame)
from llm_client import TokenMeter

fails = []


def check(name, cond, extra=""):
    print("  %s %s%s" % ("OK " if cond else "FAIL", name,
                         "" if cond else "  <<< " + str(extra)[:300]))
    if not cond:
        fails.append(name)


ROLES8 = [ROLE_WOLF, ROLE_WOLF_KING, ROLE_WHITE_WOLF_KING, ROLE_WITCH,
          ROLE_HUNTER, ROLE_SEER, ROLE_GUARD, ROLE_VILLAGER]
LOG = []
ASKED = {}


class Stub(object):
    def __init__(self, seat, pick):
        self.seat, self.pick, self.label = seat, pick, "stub"

    def chat(self, system, user, expect_json=False):
        if "是否自爆" in user or "请决定现在是否自爆" in user:
            ASKED[self.seat] = ASKED.get(self.seat, 0) + 1
        return self.pick(self.seat, user), {"prompt": 5, "completion": 5,
                                            "estimated": True}


def make(pick, settings=None, seed=4):
    g = WerewolfGame({}, {}, lambda k, p: LOG.append((k, dict(p) if isinstance(p, dict) else p)),
                     TokenMeter(), speed=0.0, seed=seed, roles=ROLES8,
                     settings=settings)
    g.judge = Stub(0, lambda se, u: "请继续。")
    for seat in g.seats:
        g.players[seat].agent = Stub(seat, pick)
    g.setup()
    return g


def never_explode(seat, user):
    if "请决定现在是否自爆" in user:
        return '{"explode": false, "speech": ""}'
    if "轮到你发言" in user:
        return '{"speech": "我是好人，先听后置位。", "suspect": 1}'
    if "请投票决定本轮" in user:
        return '{"vote": 2, "speech": "我投他。"}'
    if "是否参加警长竞选" in user:
        return '{"run": false, "speech": "不上警。"}'
    return '{"speech": "……"}'


print("=== 1. 同一天每只狼只被问一次 ===")
LOG[:] = []
ASKED.clear()
g = make(never_explode, settings=dict(G.DEFAULT_SETTINGS, sheriff=False))
g.round_no = 1
g.day_phase()
wolves = [s for s in g.seats if g.players[s].is_wolf]
print("    狼席位=%s 各自被问次数=%s" % (wolves, {s: ASKED.get(s, 0) for s in wolves}))
check("每只狼当天只被问恰好一次",
      all(ASKED.get(s, 0) == 1 for s in wolves), ASKED)
check("没有狼自爆", g.self_exploded is False)
# 统计当天总询问次数（应为狼数，而不是狼数×发言人数）
check("询问总次数 == 狼数", sum(ASKED.values()) == len(wolves),
      (sum(ASKED.values()), len(wolves)))

print("=== 2. 新的一天重新给一次机会 ===")
ASKED.clear()
g.round_no = 2
g.day_phase()
alive_wolves = [s for s in g.seats if g.players[s].is_wolf and g.players[s].alive]
print("    第2天存活狼=%s 询问次数=%s" % (alive_wolves,
                                        {s: ASKED.get(s, 0) for s in alive_wolves}))
check("第二天存活狼又被各问一次",
      alive_wolves and all(ASKED.get(s, 0) == 1 for s in alive_wolves), ASKED)
check("已出局的狼不再被问",
      all(ASKED.get(s, 0) == 0 for s in g.seats
          if g.players[s].is_wolf and not g.players[s].alive), ASKED)

print("=== 3. 狼人先自爆判定、后发言 ===")
LOG[:] = []
ASKED.clear()


def explode_when_asked(seat, user):
    if "请决定现在是否自爆" in user:
        return ('{"explode": true, "speech": "我自爆，直接进黑夜。", '
                '"take": null}')
    return never_explode(seat, user)


g2 = make(explode_when_asked, settings=dict(G.DEFAULT_SETTINGS, sheriff=False))
g2.round_no = 1
wolf0 = [s for s in g2.seats if g2.players[s].is_wolf][0]
LOG[:] = []
g2.day_phase()
chat = [p for k, p in LOG if k == "chat"]
explode_msgs = [c for c in chat if c.get("msg") == "自爆"]
speeches = [c for c in chat if c.get("msg") == "发言"]
check("出现了自爆消息", len(explode_msgs) >= 1, explode_msgs)
check("自爆者没有再产生普通发言",
      all(c.get("seat") != wolf0 for c in speeches), [c.get("seat") for c in speeches])
check("自爆台词只输出一条（不再公开层+狼队层各一遍）",
      len([c for c in chat if c.get("seat") == wolf0
           and "自爆" in (c.get("text") or "")]) == 1,
      [c for c in chat if c.get("seat") == wolf0])
check("自爆后当天不再投票（直接进黑夜）", g2.self_exploded is True)

print("=== 4. 自爆判定的提示词带上了后果与队友现状 ===")
g3 = make(never_explode)
w3 = [s for s in g3.seats if g3.players[s].is_wolf][0]
captured = []


class Capture(Stub):
    def chat(self, system, user, expect_json=False):
        captured.append(user)
        return super(Capture, self).chat(system, user, expect_json)


g3.players[w3].agent = Capture(w3, never_explode)
g3.round_no = 1
g3._ask_explode(g3.players[w3])
u = captured[-1]
check("提示词说明自爆后果（好人立即获胜）", "好人阵营立即获胜" in u, u[-400:])
check("提示词给出存活狼队友", "当前存活狼队友" in u, u[-400:])
check("提示词说明只有一次机会", "只有这一次" in u, u[-400:])
check("提示词里有当前存活名单（防止引用已出局的人）", "存活玩家" in u, u[-400:])

print("=== 5. 白狼王/狼王的自爆提示区分正确 ===")
g4 = make(never_explode)
wwk = [s for s in g4.seats if g4.players[s].role == ROLE_WHITE_WOLF_KING][0]
cap2 = []


class Capture2(Stub):
    def chat(self, system, user, expect_json=False):
        cap2.append(user)
        return super(Capture2, self).chat(system, user, expect_json)


g4.players[wwk].agent = Capture2(wwk, never_explode)
g4._ask_explode(g4.players[wwk])
check("白狼王提示可以带走一人", "可以带走任意一名存活玩家" in cap2[-1], cap2[-1][-300:])
wk = [s for s in g4.seats if g4.players[s].role == ROLE_WOLF_KING][0]
cap2[:] = []
g4.players[wk].agent = Capture2(wk, never_explode)
g4._ask_explode(g4.players[wk])
check("狼王提示自爆不开枪", "不会触发你的开枪技能" in cap2[-1], cap2[-1][-300:])

print()
print("失败项 %d：%s" % (len(fails), fails))
print("EXPLODE-POLICY TEST", "OK" if not fails else "FAILED")
sys.exit(0 if not fails else 1)
