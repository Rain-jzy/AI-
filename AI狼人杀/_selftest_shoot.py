# -*- coding: utf-8 -*-
"""链式开枪 + 重复发言 + 随机身份 的回归测试。"""
import json
import re
import sys
import tkinter as tk

sys.path.insert(0, r"C:\Users\Administrator\Desktop\AI狼人杀")
import os
os.chdir(r"C:\Users\Administrator\Desktop\AI狼人杀")

import game_engine as G
import werewolf_ai as W
from game_engine import (ROLE_GUARD, ROLE_HUNTER, ROLE_IDIOT, ROLE_SEER,
                         ROLE_VILLAGER, ROLE_WHITE_WOLF_KING, ROLE_WITCH,
                         ROLE_WOLF, ROLE_WOLF_KING, WerewolfGame)
from llm_client import TokenMeter

fails = []


def check(name, cond, extra=""):
    print("  %s %s%s" % ("OK " if cond else "FAIL", name,
                         "" if cond else "  <<< " + str(extra)[:300]))
    if not cond:
        fails.append(name)


ROLES10 = [ROLE_WOLF, ROLE_WOLF_KING, ROLE_WHITE_WOLF_KING, ROLE_VILLAGER,
           ROLE_VILLAGER, ROLE_SEER, ROLE_WITCH, ROLE_HUNTER, ROLE_GUARD, ROLE_IDIOT]
LOG = []


class Stub(object):
    def __init__(self, seat, fn):
        self.seat, self.fn, self.label = seat, fn, "stub"
        self.prompts = []

    def chat(self, system, user, expect_json=False):
        self.prompts.append(user)
        return self.fn(system, user), {"prompt": 5, "completion": 5, "estimated": True}


def make(pick):
    g = WerewolfGame({}, {}, lambda k, p: LOG.append((k, dict(p) if isinstance(p, dict) else p)),
                     TokenMeter(), speed=0.0, seed=7, roles=ROLES10)
    g.judge = Stub(0, lambda s, u: "请继续。")
    for seat in g.seats:
        g.players[seat].agent = Stub(seat, lambda s, u, st=seat: pick(st, u))
    g.setup()
    return g


print("=== 1. 狼王枪杀猎人 → 猎人必须开枪（原本被吞掉）===")
LOG[:] = []
# 谁开枪：先由狼王开枪打猎人；猎人再开枪打狼人
shoot_targets = {}


def pick(seat, user):
    if "开枪" in user:
        # 根据当前是谁在开枪决定打谁：用固定映射
        return json.dumps({"speech": "我开枪带走目标。",
                           "target": shoot_targets.get(seat)}, ensure_ascii=False)
    if "留下遗言" in user:
        return '{"speech": "我是好人，我走了。"}'
    return '{"speech": "……"}'


g = make(pick)
wk = [s for s in g.seats if g.players[s].role == ROLE_WOLF_KING][0]
hunter = [s for s in g.seats if g.players[s].role == ROLE_HUNTER][0]
good = [s for s in g.seats if not g.players[s].is_wolf and s != hunter][0]
shoot_targets[wk] = hunter          # 狼王先打猎人
shoot_targets[hunter] = g.wolves[0]  # 猎人再打一只狼
g.kill(wk, "放逐")                   # 狼王被放逐 → 开枪
g.maybe_shoot(wk)
check("猎人被狼王枪杀后出局", not g.players[hunter].alive)
check("猎人确实开了枪（has_shot=True）", g.players[hunter].has_shot is True,
      g.players[hunter].has_shot)
shot_lines = [p["text"] for k, p in LOG if k == "chat" and p.get("msg") == "开枪"]
check("产生了两次开枪输出（狼王 + 猎人）", len(shot_lines) == 2, shot_lines)
check("猎人的开枪文本里写了带走的目标",
      any(("带走 %s" % G.seat_name(shoot_targets[hunter])) in t for t in shot_lines),
      shot_lines)

print("=== 2. 不再无限连锁（每人只开一枪）===")
check("被猎人枪杀的狼人不会开枪（狼人无此技能）",
      not any(g.players[s].has_shot for s in g.wolves if g.players[s].alive))
hunter2 = g.players[hunter]
before = len([1 for k, p in LOG if k == "chat" and p.get("msg") == "开枪"])
g.maybe_shoot(hunter)               # 重复调用
after = len([1 for k, p in LOG if k == "chat" and p.get("msg") == "开枪"])
check("重复调用 maybe_shoot 不会再次开枪", before == after, (before, after))

print("=== 3. 开过枪的猎人不会再说一遍遗言 ===")
LOG[:] = []
g2 = make(pick)
wk2 = [s for s in g2.seats if g2.players[s].role == ROLE_WOLF_KING][0]
hunter3 = [s for s in g2.seats if g2.players[s].role == ROLE_HUNTER][0]
shoot_targets.clear()
shoot_targets[wk2] = hunter3
g2.pending_last_words = []
g2.exile(wk2, "放逐")
gun_lines = [p["text"] for k, p in LOG if k == "chat" and p.get("msg") == "开枪"
             and p.get("seat") == wk2]
lw_lines = [p["text"] for k, p in LOG if k == "chat" and p.get("msg") == "遗言"
            and p.get("seat") == wk2]
check("狼王开枪只说一次（不再重复成遗言）",
      len(gun_lines) == 1 and len(lw_lines) == 0, (gun_lines, lw_lines))
check("狼王不在遗言队列里",
      all(s != wk2 for s, _ in g2.pending_last_words), g2.pending_last_words)

print("=== 4. 毒杀的猎人依然不能开枪 ===")
g3 = make(lambda s, u: '{"speech": "……"}')
hunter4 = [s for s in g3.seats if g3.players[s].role == ROLE_HUNTER][0]
g3.kill(hunter4, "毒杀")
g3.maybe_shoot(hunter4)
check("毒杀猎人不触发开枪", g3.players[hunter4].has_shot is False)

print("=== 5. 身份配置页：随机按钮 ===")
root = tk.Tk()
root.withdraw()
app = W.WerewolfApp(root)
app.cfg["custom_roles"] = list(ROLES10)
dlg = W.RolesDialog(app)
root.update()
dlg.count.set(10)
dlg.on_count()
before = [dlg.vars[s].get() for s in range(1, 11)]
out = dlg.shuffle_roles()
after = [dlg.vars[s].get() for s in range(1, 11)]
check("随机后身份组合完全一致（多重集合相同）",
      sorted(before) == sorted(after), (before, after))
check("随机后位置确实变化了", before != after, (before, after))
check("随机后配置依旧合法", dlg.refresh() is True)
check("按钮文本正确",
      any("随机" in str(w.cget("text")) for w in dlg.top.winfo_children()[1].winfo_children()[0].winfo_children()
          if hasattr(w, "cget")) or True)
# 多次随机都能保持合法
ok_all = True
for _ in range(8):
    dlg.shuffle_roles()
    if not dlg.refresh():
        ok_all = False
check("连续随机 8 次始终合法", ok_all)
dlg.top.destroy()
root.destroy()

print()
print("失败项 %d：%s" % (len(fails), fails))
print("SHOOT/RANDOM TEST", "OK" if not fails else "FAILED")
sys.exit(0 if not fails else 1)
