# -*- coding: utf-8 -*-
"""本地模拟 LLM 服务：用于离线测试狼人杀引擎（返回符合格式的随机决策）。"""
import json
import random
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SPEECHES = [
    "昨晚的情况我记下了，%s 的位置我总觉得有问题，先记他一票。",
    "我是好人，我的逻辑是：%s 发言太飘，需要重点观察。",
    "我先表个态，我认为 %s 大概率有问题，大家跟我的票走。",
    "别急着投，听我分析：%s 昨晚的表情不对，我怀疑他。",
    "我悍跳一下，我是预言家，%s 是查杀。",
]
PICK = re.compile(r"(\d+)号")


def make_response(prompt):
    seats = [int(x) for x in re.findall(r"(\d+)号", prompt)]
    seats = [s for s in seats if 1 <= s <= 11] or [1]
    low = prompt

    # 上帝播报 / 绰号
    if "【上帝播报台账】" in low:
        return "天亮了，请睁眼。这一轮请各位畅所欲言。"
    if "绰号" in low:
        return json.dumps({"nickname": random.choice(
            ["人狠话不多", "逻辑狂魔", "复读机", "谜语人", "带刀侍卫", "佛系选手"])},
            ensure_ascii=False)

    target = random.choice(seats)

    if '"explode"' in low:
        return json.dumps({"explode": False, "speech": "我先不自爆，正常发言。",
                           "take": None}, ensure_ascii=False)
    if '"run"' in low:
        run = random.random() < 0.5
        return json.dumps({"run": run,
                           "speech": ("我上警，我要带节奏。" if run else "我不上警，先看戏。")},
                          ensure_ascii=False)
    if '"use_antidote"' in low:
        return json.dumps({"speech": "我考虑一下用药。", "use_antidote": False,
                           "use_poison": False, "poison": None}, ensure_ascii=False)
    if '"suspect"' in low and '"speech"' in low:
        return json.dumps({"speech": random.choice(SPEECHES) % ("%d号" % target),
                           "suspect": target}, ensure_ascii=False)
    if '"vote"' in low:
        return json.dumps({"vote": target, "speech": "我投%d号。" % target},
                          ensure_ascii=False)
    if '"target"' in low and '"speech"' in low:
        return json.dumps({"speech": random.choice(SPEECHES) % ("%d号" % target),
                           "target": target}, ensure_ascii=False)
    if '"speech"' in low:
        return json.dumps({"speech": random.choice(SPEECHES) % ("%d号" % target)},
                          ensure_ascii=False)
    return json.dumps({"speech": "我保持沉默。"}, ensure_ascii=False)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n).decode("utf-8"))
        text = "\n".join(m.get("content", "") for m in body.get("messages", []))
        out = make_response(text)
        time.sleep(0.01)
        data = {
            "id": "mock", "object": "chat.completion", "model": body.get("model"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": out},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": max(1, len(text) // 3),
                      "completion_tokens": max(1, len(out) // 3),
                      "total_tokens": 0},
        }
        raw = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def serve(port=8777):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    serve()
    print("mock llm on 8777")
    while True:
        time.sleep(1)
