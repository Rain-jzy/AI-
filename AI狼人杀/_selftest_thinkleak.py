# -*- coding: utf-8 -*-
"""思考残留过滤回归测试。

用用户截图里的真实失败文本作为用例，验证：
  * 纯英文思考链不会被当成发言
  * "英文思考 + 少量中文" 只保留中文台词
  * 正常的 JSON / 中文发言照常通过
  * 未勾选思考模式时请求体里带 thinking=disabled
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, r"C:\Users\Administrator\Desktop\AI狼人杀")

from llm_client import LLMAgent, TokenMeter
from game_engine import WerewolfGame, ROLE_WITCH, ROLE_VILLAGER

fails = []


def check(name, cond, extra=""):
    print("  %s %s%s" % ("OK " if cond else "FAIL", name,
                         "" if cond else "  <<< " + str(extra)[:260]))
    if not cond:
        fails.append(name)


# 用不跑对局的方式拿到引擎的方法（直接构造一个占位实例）
g = WerewolfGame({}, {}, lambda k, p: None, TokenMeter(), speed=0.0, seed=1)

THINKING_EN = (
    "The user is playing as seat 6, the witch. Night 1, seat 8 was killed by "
    "wolves. I need to decide: save or poison. Standard strategy: On night 1, "
    "usually save the poisoned person since it's a 10-player game. Actually "
    "many witches save on night 1. But if I save, I waste the antidote "
    "potentially on a wolf-killed villager... but standard play is to save on "
    "night 1 since I have no info. Hmm, actually with...")
THINKING_MIXED = (
    "Let me think about this as the 5号 idiot player. Facts: 8号 died by knife. "
    "1号: civilian claim, suspects 9号 before 9 speaks. This is aggressive. "
    "My read: 1号's premature targeting of 9 is odd. Actually 4号's critique is "
    "reasonable. Let me give my stance. I'm the idiot, so I want to a...")
THINKING_MIXED_CN = (
    "Let me think about this as the 3号 wolf. 局势：10号称预言家验6号好人，"
    "多人踩我，我被票出。\n"
    "Thinking: should I claim seer? No, too risky.\n"
    "我可以说：我是平民，被冤出局。我踩最凶的4、6、7、9里混着狼。")

print("=== 1. 纯英文思考链 → 不当作发言 ===")
check("识别为思考过程", g._looks_like_thinking(THINKING_EN) is True)
check("抽不出台词", g.extract_speech(THINKING_EN) is None,
      g.extract_speech(THINKING_EN))

print("=== 2. 英文思考 + 少量中文 ===")
check("识别为思考过程", g._looks_like_thinking(THINKING_MIXED) is True)
check("抽不出台词（中文太少）", g.extract_speech(THINKING_MIXED) is None,
      g.extract_speech(THINKING_MIXED))

print("=== 3. 英文思考 + 有完整中文台词 → 只留中文 ===")
out = g.extract_speech(THINKING_MIXED_CN)
check("抽出了中文台词", bool(out) and "我是平民" in out, out)
check("台词里没有英文思考", out and "Let me" not in out and "Thinking" not in out, out)

print("=== 4. 正常输出照常通过 ===")
check("纯 JSON 正常解析",
      g.extract_speech('{"speech": "我是预言家，昨晚验了 3号。"}')
      == "我是预言家，昨晚验了 3号。")
check("中文纯文本正常通过",
      g.extract_speech("我是平民，我先听后置位，怀疑 2号。")
      == "我是平民，我先听后置位，怀疑 2号。")
check("被截断的 JSON 也能救回台词",
      g.extract_speech('{"speech": "我投 3号，他的逻辑站不住。"') == "我投 3号，他的逻辑站不住。",
      g.extract_speech('{"speech": "我投 3号，他的逻辑站不住。"'))
check("正常中文不会被误判为思考",
      g._looks_like_thinking("我是平民，我怀疑 2号和 9号。") is False)

print("=== 5. ask_player 端到端：思考链不会变成发言 ===")


class Stub(object):
    def __init__(self, reply):
        self.reply, self.label = reply, "stub"

    def chat(self, system, user, expect_json=False):
        return self.reply, {"prompt": 1, "completion": 1, "estimated": True}


p = g.players[1]
g.setup()
p.agent = Stub(THINKING_EN)
data, raw = g.ask_player(p, "请发言。", fallback_speech="（我暂时没有明确信息）")
check("抽不到 JSON 时回落到兜底台词",
      data is None and raw == "（我暂时没有明确信息）", (data, raw))
p.agent = Stub(THINKING_MIXED_CN)
data2, raw2 = g.ask_player(p, "请发言。", fallback_speech="兜底")
check("有中文台词时取中文部分", isinstance(data2, dict)
      and "我是平民" in data2["speech"], data2)

print("=== 6. 请求体：未勾选思考模式时显式关闭 ===")
CAPTURED = []


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        CAPTURED.append(json.loads(self.rfile.read(n).decode("utf-8")))
        raw = json.dumps({"choices": [{"message": {"content": '{"speech": "好"}'}}],
                          "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
                         ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


srv = ThreadingHTTPServer(("127.0.0.1", 8815), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()

base = {"name": "t", "base_url": "http://127.0.0.1:8815/v1", "api_key": "x",
        "model": "deepseek-flash", "max_tokens": 300}
LLMAgent("1号", dict(base), TokenMeter()).chat("s", "u")
check("默认发送 thinking=disabled",
      CAPTURED[-1].get("thinking") == {"type": "disabled"}, CAPTURED[-1].get("thinking"))
check("默认不发 reasoning_effort", "reasoning_effort" not in CAPTURED[-1])
LLMAgent("2号", dict(base, thinking=True), TokenMeter()).chat("s", "u")
check("勾选后发送 thinking=enabled + reasoning_effort=high",
      CAPTURED[-1].get("thinking") == {"type": "enabled"}
      and CAPTURED[-1].get("reasoning_effort") == "high", CAPTURED[-1])

srv.shutdown()
print()
print("失败项 %d：%s" % (len(fails), fails))
print("THINKING-LEAK TEST", "OK" if not fails else "FAILED")
sys.exit(0 if not fails else 1)
