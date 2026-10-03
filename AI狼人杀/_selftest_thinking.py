# -*- coding: utf-8 -*-
"""思考模式 + 预设更新 的回归测试。

用一个本地假网关抓取请求体，验证：
  * 关闭思考模式时不发送 reasoning_effort / thinking
  * 开启思考模式时发送 reasoning_effort=high 与 thinking={"type":"enabled"}
  * 开启思考模式时不再发送 temperature 干扰（仍发送但不影响，官方会忽略）
  * reasoning_tokens 会计入消耗统计
  * 预设列表里 DeepSeek 指向 deepseek-flash
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, r"C:\Users\Administrator\Desktop\AI狼人杀")

from llm_client import PROVIDER_PRESETS, LLMAgent, TokenMeter

fails = []
CAPTURED = []


def check(name, cond, extra=""):
    print("  %s %s%s" % ("OK " if cond else "FAIL", name,
                         "" if cond else "  <<< " + str(extra)[:300]))
    if not cond:
        fails.append(name)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n).decode("utf-8"))
        CAPTURED.append(body)
        data = {
            "choices": [{"message": {"role": "assistant",
                                     "content": '{"speech": "测试"}'}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                      "completion_tokens_details": {"reasoning_tokens": 260}},
        }
        raw = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


srv = ThreadingHTTPServer(("127.0.0.1", 8811), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()

print("=== 1. 关闭思考模式（默认）===")
meter = TokenMeter()
cfg = {"name": "t", "base_url": "http://127.0.0.1:8811/v1", "api_key": "x",
       "model": "deepseek-flash", "temperature": 0.8, "max_tokens": 300}
agent = LLMAgent("1号", cfg, meter)
agent.chat("sys", "user")
body = CAPTURED[-1]
check("未发送 reasoning_effort", "reasoning_effort" not in body, body.keys())
check("显式发送 thinking=disabled（防止默认带思考）",
      body.get("thinking") == {"type": "disabled"}, body.get("thinking"))
check("模型名为 deepseek-flash", body.get("model") == "deepseek-flash", body.get("model"))
check("仍发送 temperature", body.get("temperature") == 0.8, body.get("temperature"))
check("推理 token 未计入（本次 20）", meter.by_agent["1号"]["completion"] == 20,
      meter.by_agent["1号"])

print("=== 2. 开启思考模式 ===")
meter2 = TokenMeter()
cfg2 = dict(cfg, thinking=True)
agent2 = LLMAgent("2号", cfg2, meter2)
agent2.chat("sys", "user")
body2 = CAPTURED[-1]
check("发送 reasoning_effort=high", body2.get("reasoning_effort") == "high",
      body2.get("reasoning_effort"))
check("发送 thinking={'type':'enabled'}",
      body2.get("thinking") == {"type": "enabled"}, body2.get("thinking"))
check("reasoning_tokens(260) 计入消耗",
      meter2.by_agent["2号"]["completion"] == 260, meter2.by_agent["2号"])
check("总消耗按推理 token 计算", meter2.total_completion == 260, meter2.total_completion)

print("=== 3. 预设列表 ===")
ds = [p for p in PROVIDER_PRESETS if p[0].startswith("DeepSeek")]
check("存在 DeepSeek 预设", len(ds) >= 1, ds)
check("主预设指向 deepseek-flash",
      any(p[2] == "deepseek-flash" for p in ds), ds)
check("仍保留旧别名条目（带弃用提示）",
      any("弃用" in p[0] for p in ds), [p[0] for p in ds])

print("=== 4. UI：对话框里的思考模式开关 ===")
import tkinter as tk
import os
os.chdir(r"C:\Users\Administrator\Desktop\AI狼人杀")
import werewolf_ai as W

root = tk.Tk()
root.withdraw()
app = W.WerewolfApp(root)
app.cfg["seats"]["3"].update({"model": "deepseek-flash",
                              "base_url": "http://127.0.0.1:8811/v1",
                              "api_key": "x", "thinking": True})
dlg = W.ConfigDialog(app)
root.update()
dlg._select(3)
check("进入 3 号时勾选框反映配置", dlg.var_thinking.get() is True)
rows = [dlg.tree.item(i, "values")[1] for i in dlg.tree.get_children()]
check("表格里显示 🧠思考 标记", any("🧠思考" in r for r in rows), rows[:4])
dlg._select(4)
check("4 号（未开思考）勾选框为 False", dlg.var_thinking.get() is False)
dlg.var_thinking.set(True)
dlg._apply_fields()
check("勾选后写回配置", app.cfg["seats"]["4"].get("thinking") is True)
agent4 = W.LLMAgent("4号", dict(app.cfg["seats"]["4"]), W.TokenMeter())
check("写回的配置能生成思考模式 agent", agent4.thinking is True)
dlg.top.destroy()
root.destroy()

srv.shutdown()
print()
print("失败项 %d：%s" % (len(fails), fails))
print("THINKING TEST", "OK" if not fails else "FAILED")
sys.exit(0 if not fails else 1)
