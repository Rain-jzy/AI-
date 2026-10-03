# -*- coding: utf-8 -*-
"""最后一次：端到端确认台词提取改动没有误伤正常发言，且对局能正常跑完。"""
import json
import os
import re
import sys
import time
import tkinter as tk

sys.path.insert(0, r"C:\Users\Administrator\Desktop\AI狼人杀")
os.chdir(r"C:\Users\Administrator\Desktop\AI狼人杀")

import game_engine as G
import werewolf_ai as W
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
    """模拟"带思考的 flash 模型"：返回 英文推理 + 中文台词。"""

    def __init__(self, seat):
        self.seat, self.label = seat, "stub-flash"

    def chat(self, system, user, expect_json=False):
        import re
        if "狼人夜间密谈" in user:
            c = re.findall(r"(\d+)号", user.split("【可选刀口】")[-1])
            txt = ('Let me think. Night kill choice: kill a good player. '
                   'Actually I should target the seer claim.\n'
                   '{} 今晚刀{}号，那个位置像个神。'.format("q", c[0]))
            return txt, {"prompt": 5, "completion": 5, "estimated": True}
        if "请投票决定本轮" in user:
            c = re.findall(r"(\d+)号", user.split("【可投票席位】")[-1])
            return ('Reasoning: follow the majority. I vote for the loud one.\n'
                    '我投{}号，他的逻辑站不住。'.format(c[0]),
                    {"prompt": 5, "completion": 5, "estimated": True})
        if "开枪" in user:
            return ('Thinking: shoot the wolf king suspect.\n'
                    '我开枪带走一个可疑位。', {"prompt": 5, "completion": 5,
                                              "estimated": True})
        return ('Let me think about this carefully. The situation is complex.\n'
                '我是好人，先听后置位，我怀疑一下前面的位置。',
                {"prompt": 5, "completion": 5, "estimated": True})


print("=== 1. 引擎：带思考的输出被正确清洗 ===")
g = WerewolfGame({}, {}, lambda k, p: LOG.append((k, p)), TokenMeter(),
                 speed=0.0, seed=5, roles=ROLES8)
g.judge = Stub(0)
for seat in g.seats:
    g.players[seat].agent = Stub(seat)
g.setup()
p = g.players[3]
data, raw = g.ask_player(p, "请投票决定本轮。", fmt_seats=[1, 2, 4],
                         fallback_speech="（兜底）")
check("从英文推理里救回了中文台词",
      isinstance(data, dict) and "我投" in data.get("speech", ""), data)
check("台词里没有英文残留",
      isinstance(data, dict) and "Reasoning" not in data["speech"]
      and "Let me" not in data["speech"], data)
check("短中文台词不会被误伤（狼队密谈）",
      g.extract_speech("我建议刀 6号，他像神。") == "我建议刀 6号，他像神。",
      g.extract_speech("我建议刀 6号，他像神。"))

print("=== 2. 完整对局（全席位都是这种带思考的假模型）===")
LOG[:] = []
g2 = WerewolfGame({}, {}, lambda k, p: LOG.append((k, p)), TokenMeter(),
                  speed=0.0, seed=9, roles=ROLES8,
                  settings=dict(G.DEFAULT_SETTINGS, last_words=True, sheriff=True))
g2.judge = Stub(0)
for seat in g2.seats:
    g2.players[seat].agent = Stub(seat)
g2.setup()
err = None
try:
    for r in range(1, 7):
        if g2.winner:
            break
        g2.round_no = r
        g2.night_phase()
        if g2.check_win():
            break
        g2.day_phase()
        if g2.check_win():
            break
except Exception:
    import traceback
    err = traceback.format_exc()
check("多轮对局无异常", err is None, (err or "")[:200])
speeches = [p["text"] for k, p in LOG if k == "chat" and p.get("layer") == "public"
            and p.get("seat") is not None]
bad = [s for s in speeches if "Let me" in s or "Thinking:" in s
       or "Reasoning:" in s or "The user" in s]
check("所有公开发言都不含英文思考链", not bad, bad[:2])
cn_ok = [s for s in speeches if len(re.findall(r"[\u4e00-\u9fff]", s)) >= 6]
check("绝大多数发言是有效中文台词", len(cn_ok) >= len(speeches) * 0.8,
      "%d/%d" % (len(cn_ok), len(speeches)))
print("  轮次=%s 胜者=%s 发言数=%d" % (g2.round_no, g2.winner, len(speeches)))

print("=== 3. 走真实 UI 跑一局（假网关，验证界面渲染）===")
import _mock_llm
srv = _mock_llm.serve(8819)
CFG = {"name": "mock", "base_url": "http://127.0.0.1:8819/v1", "api_key": "x",
       "model": "deepseek-flash", "temperature": 0.9, "max_tokens": 600,
       "timeout": 20, "retries": 2, "json_mode": False}
root = tk.Tk()
root.withdraw()
app = W.WerewolfApp(root)
for s in range(1, W.MAX_PLAYERS + 1):
    app.cfg["seats"][str(s)].update(CFG)
app.cfg["judge"].update(CFG)
for b in (app.mode_btn_custom, app.btn_roles, app.btn_settings, app.btn_cfg):
    b.configure(state="normal")
app.cfg["mode"] = "custom"
app.cfg["custom_roles"] = list(ROLES8)
app.cfg["settings"] = dict(W.DEFAULT_SETTINGS)
app.speed_var.set(0.0)
root.update()
app.start_game()
t0 = time.time()
while app.game is not None and time.time() - t0 < 300:
    app._pump()
    root.update()
    time.sleep(0.03)
    if app.game is None:
        break
txt = app.chat.get("1.0", "end")
print("  对局 %.1fs 胜者=%s 字数=%d" % (time.time() - t0, app.winner, len(txt)))
check("对局跑完并分出胜负", app.winner is not None, app.winner)
check("界面上没有英文思考链", "Let me" not in txt and "The user is playing" not in txt,
      [l for l in txt.splitlines() if "Let me" in l][:1])
check("界面渲染了发言", "[发言]" in txt or "[遗言]" in txt, len(txt))
root.destroy()
srv.shutdown()

print()
print("失败项 %d：%s" % (len(fails), fails))
print("E2E CLEAN", "OK" if not fails else "FAILED")
sys.exit(0 if not fails else 1)
