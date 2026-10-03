# -*- coding: utf-8 -*-
"""信息隔离回归测试（针对用户报告的 bug）。

核心断言：任何玩家（尤其是"还没发过言的人"和"死人"）拿到的提示词里，
绝不能出现女巫用药、守卫守人、验人结果、狼队频道、上帝视角判定等非公开内容；
同时公开宣称（有人说"我是女巫"）必须照常可见 —— 那是策略，不是泄露。
"""
import json
import re
import sys

sys.path.insert(0, r"C:\Users\Administrator\Desktop\AI狼人杀")

import game_engine as G
from game_engine import (ROLE_GUARD, ROLE_HUNTER, ROLE_IDIOT, ROLE_SEER,
                         ROLE_VILLAGER, ROLE_WHITE_WOLF_KING, ROLE_WITCH,
                         ROLE_WOLF, ROLE_WOLF_KING, WerewolfGame)
from llm_client import TokenMeter

fails = []


def check(name, cond, extra=""):
    print("  %s %s%s" % ("OK " if cond else "FAIL", name,
                         "" if cond else "  <<< " + str(extra)[:400]))
    if not cond:
        fails.append(name)


ROLES10 = [ROLE_WOLF, ROLE_WOLF_KING, ROLE_WHITE_WOLF_KING, ROLE_VILLAGER,
           ROLE_VILLAGER, ROLE_SEER, ROLE_WITCH, ROLE_HUNTER, ROLE_GUARD, ROLE_IDIOT]


class Stub(object):
    def __init__(self, seat, fn):
        self.seat, self.fn, self.label = seat, fn, "stub"
        self.prompts = []

    def chat(self, system, user, expect_json=False):
        self.prompts.append(user)
        return self.fn(system, user), {"prompt": 5, "completion": 5, "estimated": True}


def make(pick, settings=None, seed=11):
    g = WerewolfGame({}, {}, lambda k, p: None, TokenMeter(), speed=0.0, seed=seed,
                     roles=ROLES10, settings=settings)
    g.judge = Stub(0, lambda s, u: "请继续。")
    for seat in g.seats:
        g.players[seat].agent = Stub(seat, lambda s, u, st=seat: pick(st, u))
    g.setup()
    return g


def pick(seat, user):
    """女巫不用药、守卫守 1 号、狼刀 6 号（制造真实死讯）、其他人正常发言/投票。"""
    if "是否立刻自爆" in user:
        return '{"explode": false, "speech": ""}'
    if "请选择今晚要查验" in user:
        return '{"speech": "我想验一下 3号。", "target": 3}'
    if "请选择今晚要守护" in user:
        return '{"speech": "我守 1号。", "target": 1}'
    if "use_antidote" in user:
        return ('{"speech": "解药只有一瓶，我先留着不救，毒药今晚也不用。", '
                '"use_antidote": false, "use_poison": false, "poison": null}')
    if "狼人夜间密谈" in user:
        return '{"speech": "今晚刀 6号。", "target": 6}'
    if "请投票决定本轮" in user:
        return '{"vote": 2, "speech": "我怀疑 2号。"}'
    if "轮到你发言" in user:
        return '{"speech": "我是平民，我先听后置位。", "suspect": 2}'
    if "留下遗言" in user:
        return '{"speech": "我是平民，我走了。"}'
    return '{"speech": "……"}'


print("=== 1. 女巫用药内幕不得进入他人提示词 ===")
g = make(pick)
witch = [s for s in g.seats if g.players[s].role == ROLE_WITCH][0]
seer = [s for s in g.seats if g.players[s].role == ROLE_SEER][0]
guard = [s for s in g.seats if g.players[s].role == ROLE_GUARD][0]
g.round_no = 1
g.night_phase()
ctx = g.public_context()
check("公开实录里没有女巫用药内容", "解药只有一瓶" not in ctx and "不用药" not in ctx, ctx)
check("公开实录里没有守卫守人内容", "守 6号" not in ctx and "守护" not in ctx, ctx)
check("公开实录里没有验人思路", "我想验一下" not in ctx, ctx)
check("公开实录里没有狼队频道内容", "今晚刀 6号" not in ctx, ctx)
check("公开实录里没有上帝视角判定（刀口/守护选择等）",
      "刀口" not in ctx and "查验：" not in ctx, ctx)
check("死讯仍然出现在公开实录里（当众公布的事实）",
      "6号" in ctx and "出局" in ctx, ctx)
check("死讯里带了死因", "刀杀" in ctx, ctx)

print("=== 2. 未发言玩家 / 死人的完整提示词逐字符检查 ===")
for seat in g.seats:
    p = g.players[seat]
    if p.is_wolf:
        continue                     # 狼人本来就该看到狼队频道
    full = g._system_prompt(p) + "\n" + g._user_prompt(
        p, "现在轮到你发言。", fmt='{"speech": "x"}')
    bad = [k for k in ("解药只有一瓶", "今晚刀", "狼队频道", "我想验一下",
                       "守护 6号", "查验：", "同守同救") if k in full]
    check("%s（%s）提示词无泄露" % (p.name, p.role), not bad, bad)

print("=== 3. 公开宣称必须保留（这是策略，不是泄露）===")
g2 = make(pick)
p2 = g2.players[4]
g2.speech(p2, "我是女巫，我昨晚救了 6号，不信你们看票型。", kind="发言")
ctx2 = g2.public_context()
check("玩家自己说出口的宣称会留在公开实录里", "我是女巫" in ctx2, ctx2)

print("=== 4. 狼人只看得到狼队频道，且好人看不到 ===")
g3 = make(pick)
g3.round_no = 1
g3.night_phase()
wolf = [s for s in g3.seats if g3.players[s].role == ROLE_WOLF][0]
good = [s for s in g3.seats if not g3.players[s].is_wolf][0]
wolf_prompt = g3._user_prompt(g3.players[wolf], "商量刀谁。")
good_prompt = g3._user_prompt(g3.players[good], "现在轮到你发言。")
check("狼人提示词里有狼队频道", "狼队频道" in wolf_prompt and "今晚刀 6号" in wolf_prompt)
check("好人提示词里没有狼队频道", "狼队频道" not in good_prompt
      and "今晚刀 6号" not in good_prompt)

print("=== 5. 死人遗言也只依据公开信息 ===")
g4 = make(pick)
victim = [s for s in g4.seats if g4.players[s].role == ROLE_VILLAGER][0]
g4.round_no = 1
g4.kill(victim, "刀杀")
g4.pending_last_words = [(victim, 0)]
before = len(g4.players[victim].agent.prompts)
g4.flush_last_words()
lw_prompt = g4.players[victim].agent.prompts[-1]
bad4 = [k for k in ("解药只有一瓶", "今晚刀 6号", "守护 6号", "查验：") if k in lw_prompt]
check("死者遗言提示词无内幕泄露", not bad4, bad4)
check("死者遗言提示词里有死讯等公开信息",
      "天亮了" in lw_prompt or "出局" in lw_prompt or "昨夜" in lw_prompt,
      lw_prompt[:300])

print("=== 6. 无三重重复播报 ===")
events = []
g5 = WerewolfGame({}, {}, lambda k, p: events.append((k, p)), TokenMeter(),
                  speed=0.0, seed=3, roles=ROLES10)
g5.judge = Stub(0, lambda s, u: "请继续。")
for seat in g5.seats:
    g5.players[seat].agent = Stub(seat, lambda s, u: '{"speech": "……"}')
g5.setup()
g5.round_no = 1
g5._apply_night_deaths([(4, "刀杀")])
texts = [p["text"] for k, p in events if k == "chat" and p.get("layer") == "public"]
deaths = [t for t in texts if "4号" in t and "出局" in t]
check("死讯只播报一次（原本重复三次）", len(deaths) == 1, deaths)
check("播报里带死因", "刀杀" in deaths[0], deaths)
check("默认不公开身份（设置未开）", "身份" not in deaths[0], deaths)

print()
print("失败项 %d：%s" % (len(fails), fails))
print("ISOLATION TEST", "OK" if not fails else "FAILED")
sys.exit(0 if not fails else 1)
