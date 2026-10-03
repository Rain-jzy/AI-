# -*- coding: utf-8 -*-
"""弃票功能 + 新默认值 回归测试。"""
import json
import re
import sys
import tkinter as tk
import os

sys.path.insert(0, r"C:\Users\Administrator\Desktop\AI狼人杀")
os.chdir(r"C:\Users\Administrator\Desktop\AI狼人杀")

import game_engine as G
from game_engine import (ROLE_GUARD, ROLE_HUNTER, ROLE_IDIOT, ROLE_SEER,
                         ROLE_VILLAGER, ROLE_WITCH, ROLE_WOLF, ROLE_WOLF_KING,
                         WerewolfGame)
from llm_client import TokenMeter

fails = []


def check(name, cond, extra=""):
    print("  %s %s%s" % ("OK " if cond else "FAIL", name,
                         "" if cond else "  <<< " + str(extra)[:300]))
    if not cond:
        fails.append(name)


ROLES8 = [ROLE_WOLF, ROLE_WOLF_KING, ROLE_VILLAGER, ROLE_VILLAGER, ROLE_WITCH,
          ROLE_HUNTER, ROLE_SEER, ROLE_GUARD]
LOG = []


class Stub(object):
    def __init__(self, seat, pick):
        self.seat, self.pick, self.label = seat, pick, "stub"

    def chat(self, system, user, expect_json=False):
        return self.pick(self.seat, user), {"prompt": 5, "completion": 5,
                                            "estimated": True}


def make(pick, settings=None):
    g = WerewolfGame({}, {}, lambda k, p: LOG.append((k, dict(p) if isinstance(p, dict) else p)),
                     TokenMeter(), speed=0.0, seed=3, roles=ROLES8,
                     settings=settings)
    g.judge = Stub(0, lambda se, u: '{"speech": "请继续。"}' if False else "请继续。")
    for seat in g.seats:
        g.players[seat].agent = Stub(seat, pick)
    g.setup()
    return g


print("=== 1. 默认值 ===")
s = G.normalize_settings({})
check("弃票默认开启", s["allow_abstain"] is True)
check("自爆默认开启", s["wolf_explode"] is True)
check("奶穿默认开启", s["guard_double"] is True)
check("女巫刀口默认开启", s["witch_know_kill"] is True)
check("女巫双药默认开启", s["witch_two_potions"] is True)
check("警长默认开启", s["sheriff"] is True)
check("遗言默认开启", s["last_words"] is True)
check("仅「公布身份死因」默认关闭",
      [k for k, v in s.items() if v is False] == ["reveal_death"], s)

print("=== 2. 全员弃票 → 无人出局 ===")
LOG[:] = []
g = make(lambda seat, user: '{"vote": null, "speech": "我弃票。"}')
out = g.vote_round()
check("全员弃票时无人出局", out is None, out)
pub = " ".join(p["text"] for k, p in LOG if k == "chat" and p.get("layer") == "public")
check("播报了全员弃票", "弃票" in pub, pub[:200])
vr = [p for k, p in LOG if k == "vote_result"]
check("vote_result 事件带 abstain 字段", vr and vr[-1].get("abstain"), vr[-1] if vr else None)

print("=== 3. 弃票 + 有效票正常结算 ===")
LOG[:] = []
g = make(lambda seat, user: ('{"vote": null, "speech": "我弃票。"}' if seat % 3 == 0
                             else '{"vote": %d, "speech": "我投他。"}' % ((seat % 5) + 1)))
g.vote_round()
votes_ev = [p for k, p in LOG if k == "vote"]
results = [p for k, p in LOG if k == "vote_result"]
last = results[-1]
abstain = last.get("abstain") or []
check("有玩家弃票且被记录", len(abstain) >= 1, abstain)
# 弃票名单里的人不应同时出现在票数里（自己弃票又被人投的情况会被排除）
abstain_in_tally = [s for s in abstain if s in (last.get("tally") or {})]
check("弃票者不出现在同一次计票的票数里", not abstain_in_tally,
      "abstain=%s tally=%s" % (abstain, last.get("tally")))
check("弃票事件 target 为 None 且标记 abstain",
      all(v.get("target") is None for v in votes_ev if v.get("abstain")), votes_ev[:4])
check("弃票数 + 得票票数 <= 有投票权的人数",
      sum(last.get("tally", {}).values()) + len(abstain) <= len(g.alive_voters()),
      (last.get("tally"), abstain, len(g.alive_voters())))

print("=== 4. 平票判定：只看候选人之间是否并列 ===")
LOG[:] = []
g = make(lambda seat, user: '{"vote": null, "speech": "弃票"}')
alive = g.alive_seats()
v = {alive[0]: alive[1], alive[1]: None, alive[2]: None}
g._settle_votes(v, tag="放逐", voters=alive[:3])
res = [p for k, p in LOG if k == "vote_result"][-1]
pub = " ".join(p["text"] for k, p in LOG if k == "chat" and p.get("layer") == "public")
check("最高票唯一（1票）→ 直接出局，弃票不参与比较",
      res.get("out") == alive[1], (pub[-160:], res.get("out")))
LOG[:] = []
g = make(lambda seat, user: '{"vote": null, "speech": "弃票"}')
alive = g.alive_seats()
v = {alive[0]: alive[1], alive[1]: alive[2], alive[3]: None, alive[4]: None}
g._settle_votes(v, tag="放逐", voters=alive[:3] + alive[3:5])
pub = " ".join(p["text"] for k, p in LOG if k == "chat" and p.get("layer") == "public")
check("两名候选人各 1 票 → 平票",
      "平票" in pub, pub[-200:])

print("=== 5. 关闭弃票后，null 会回退成有效票 ===")
LOG[:] = []
g = make(lambda seat, user: '{"vote": null, "speech": "随便。"}',
         settings=dict(G.DEFAULT_SETTINGS, allow_abstain=False))
g.vote_round()
tally = [p for k, p in LOG if k == "vote_result"][-1]
votes_ev = [p for k, p in LOG if k == "vote"]
check("关闭弃票时不会出现 abstain", not (tally.get("abstain")), tally)
check("关闭弃票时每张票都落到具体席位（无 null）",
      votes_ev and all(v.get("target") is not None for v in votes_ev), votes_ev[:3])
check("关闭弃票时计票里有具体候选人", bool(tally.get("tally")), tally)

print("=== 6. 警长投票也支持弃票 ===")
LOG[:] = []
g = make(lambda seat, user: ('{"run": true, "speech": "我上警。"}'
                             if seat in (1, 2) else
                             ('{"vote": null, "speech": "我弃票。"}'
                              if "警长" in user or "候选人" in user else
                              '{"run": false, "speech": "不上警。"}')))
g.round_no = 1
g.sheriff_election()
pub = " ".join(p["text"] for k, p in LOG if k == "chat" and p.get("layer") == "public")
check("警长竞选仍能选出警长", g.sheriff_seat in (1, 2), g.sheriff_seat)
check("警长投票的弃票被公布", "弃票" in pub, pub[-200:])

print("=== 7. UI：弃票渲染与设置项 ===")
import werewolf_ai as W
# 清掉可能残留的本地配置，保证测的是"全新安装"的默认值
if os.path.exists(W.CONFIG_PATH):
    os.remove(W.CONFIG_PATH)
root = tk.Tk()
root.withdraw()
app = W.WerewolfApp(root)
check("全新配置的默认值与引擎一致（仅 reveal_death 关）",
      [k for k, v in app.cfg["settings"].items() if v is False] == ["reveal_death"],
      app.cfg["settings"])
app.roles = {1: ROLE_WOLF}
app._handle({"kind": "vote", "voter": 1, "target": None, "abstain": True,
             "reason": "我不想投", "round": 1})
root.update()
txt = app.chat.get("1.0", "end")
check("界面把弃票渲染成「1号 弃票」", "1号 弃票" in txt, txt[-80:])
app._handle({"kind": "vote_result", "tally": {2: 3.0}, "out": 2, "tag": "放逐",
             "finalists": [], "abstain": [1, 5]})
root.update()
txt2 = app.chat.get("1.0", "end")
check("计票横幅里显示弃票名单", "弃票：1号、5号" in txt2, txt2[-160:])
dlg = W.SettingsDialog(app)
root.update()
check("设置窗口里有弃票开关", "allow_abstain" in dlg.vars, list(dlg.vars))
check("弃票开关默认勾选", dlg.vars["allow_abstain"].get() is True)
check("公布身份死因默认未勾选", dlg.vars["reveal_death"].get() is False)
check("其余开关默认勾选",
      all(dlg.vars[k].get() for k in dlg.vars if k != "reveal_death"),
      {k: v.get() for k, v in dlg.vars.items()})
dlg.vars["allow_abstain"].set(False)
dlg.save()
check("取消勾选后写回配置", app.cfg["settings"]["allow_abstain"] is False)
dlg.top.destroy()
root.destroy()

print()
print("失败项 %d：%s" % (len(fails), fails))
print("ABSTAIN TEST", "OK" if not fails else "FAILED")
sys.exit(0 if not fails else 1)
