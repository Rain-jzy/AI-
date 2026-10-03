# -*- coding: utf-8 -*-
"""
llm_client.py —— OpenRouter / OpenAI 兼容接口客户端
=================================================
* 只依赖 Python 标准库（urllib），无需 pip 安装任何东西。
* 支持任意 OpenAI 兼容的 /chat/completions 服务（OpenRouter、DeepSeek、通义、
  Kimi、智谱、硅基流动、本地 ollama / vLLM 等）。
* 自动统计 token 消耗（优先后端返回的 usage，缺失时按字符估算）。
* 自动重试 + 从自然语言回复中稳健地抽取 JSON 动作。
"""

import json
import random
import re
import time
import urllib.error
import urllib.request

# ----------------------------------------------------------------------------
# 常用服务商预设：name -> (base_url, 示例模型)
# base_url 统一填写到 /v1 这一层，程序会自动拼接 /chat/completions
# ----------------------------------------------------------------------------
PROVIDER_PRESETS = [
    ("OpenRouter", "https://openrouter.ai/api/v1", "deepseek/deepseek-chat-v3.1"),
    ("DeepSeek 官方 · V4.1-Flash", "https://api.deepseek.com/v1", "deepseek-flash"),
    ("DeepSeek 官方 · 旧别名(已弃用)", "https://api.deepseek.com/v1", "deepseek-chat"),
    ("阿里通义百炼", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    ("月之暗面 Kimi", "https://api.moonshot.cn/v1", "kimi-k2-0905-preview"),
    ("智谱 GLM", "https://open.bigmodel.cn/api/paas/v4", "glm-4-plus"),
    ("硅基流动", "https://api.siliconflow.cn/v1", "deepseek-ai/DeepSeek-V3"),
    ("本地 ollama", "http://127.0.0.1:11434/v1", "qwen2.5:7b"),
    ("自定义", "", ""),
]


class LLMError(Exception):
    """一次模型调用彻底失败（重试后仍然失败）。"""


class TokenMeter(object):
    """全局 token 计量器，同时按“席位/AI”累计。"""

    def __init__(self):
        self.total_prompt = 0
        self.total_completion = 0
        self.total_calls = 0
        self.total_errors = 0
        self.by_agent = {}          # key -> dict(prompt, completion, calls, errors)
        self.per_call = []          # 最近若干次调用的记录，用于 UI 明细

    def _bucket(self, key):
        if key not in self.by_agent:
            self.by_agent[key] = {"prompt": 0, "completion": 0, "calls": 0, "errors": 0}
        return self.by_agent[key]

    def add(self, key, prompt_tokens, completion_tokens, estimated=False):
        self.total_prompt += prompt_tokens
        self.total_completion += completion_tokens
        self.total_calls += 1
        b = self._bucket(key)
        b["prompt"] += prompt_tokens
        b["completion"] += completion_tokens
        b["calls"] += 1
        self.per_call.append({
            "key": key,
            "prompt": prompt_tokens,
            "completion": completion_tokens,
            "total": prompt_tokens + completion_tokens,
            "estimated": estimated,
            "time": time.strftime("%H:%M:%S"),
        })
        if len(self.per_call) > 500:
            del self.per_call[:-500]

    def add_error(self, key):
        self.total_errors += 1
        self._bucket(key)["errors"] += 1

    @property
    def total(self):
        return self.total_prompt + self.total_completion

    def agent_total(self, key):
        b = self.by_agent.get(key)
        if not b:
            return 0
        return b["prompt"] + b["completion"]

    def summary_text(self):
        return ("总消耗 %s tokens（输入 %s / 输出 %s）｜调用 %d 次｜失败 %d 次"
                % (fmt_num(self.total), fmt_num(self.total_prompt),
                   fmt_num(self.total_completion), self.total_calls, self.total_errors))


def fmt_num(n):
    """1234567 -> 1,234,567"""
    return "{:,}".format(int(n))


def estimate_tokens(text):
    """粗略估算 token：中文约 1 字 ≈ 1 token，英文约 4 字符 ≈ 1 token。"""
    if not text:
        return 0
    cjk = len(re.findall(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]", text))
    other = len(text) - cjk
    return int(cjk + other / 4.0) + 1


# ----------------------------------------------------------------------------
# JSON 抽取
# ----------------------------------------------------------------------------
def extract_json(text):
    """从模型输出里尽力抽取一个 JSON 对象，失败返回 None。"""
    if not text:
        return None
    s = text.strip()

    # 去掉 ```json ... ``` 代码块围栏
    fence = re.search(r"```(?:json|JSON)?\s*(.*?)```", s, re.S)
    if fence:
        s = fence.group(1).strip()

    candidates = [s]
    # 第一个 { 到最后一个 }
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j > i:
        candidates.append(s[i:j + 1])
    # 平衡括号扫描，取第一个完整对象
    depth, start = 0, None
    for idx, ch in enumerate(s):
        if ch == "{":
            if depth == 0:
                start = idx
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    candidates.append(s[start:idx + 1])
                    start = None

    for cand in candidates:
        cand = cand.strip()
        if not cand.startswith("{"):
            continue
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except Exception:
            # 常见瑕疵修复：单引号、尾随逗号、中文引号
            fixed = cand.replace("'", '"')
            fixed = re.sub(r",\s*([}\]])", r"\1", fixed)
            fixed = fixed.replace("“", '"').replace("”", '"').replace("，", ",")
            try:
                obj = json.loads(fixed)
                if isinstance(obj, dict):
                    return obj
            except Exception:
                continue
    return None


def pick_seat(value, valid):
    """把模型给出的任意写法（"3"、"3号"、"玩家3"、3）转成合法席位号。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value in valid else None
    m = re.search(r"\d+", str(value))
    if not m:
        return None
    n = int(m.group(0))
    return n if n in valid else None


# ----------------------------------------------------------------------------
# 单个 AI 席位客户端
# ----------------------------------------------------------------------------
class LLMAgent(object):
    """一个 AI 席位：独立的名字 / 模型 / 温度 / 密钥，并共享一个 TokenMeter。"""

    def __init__(self, key, config, meter):
        self.key = key                    # 席位标识，如 "3号" 或 "上帝"
        self.meter = meter
        self.name = config.get("name") or key
        self.base_url = (config.get("base_url") or "").strip()
        self.api_key = (config.get("api_key") or "").strip()
        self.model = (config.get("model") or "").strip()
        self.temperature = float(config.get("temperature", 0.8) or 0.8)
        self.max_tokens = int(config.get("max_tokens", 800) or 800)
        self.timeout = int(config.get("timeout", 120) or 120)
        self.json_mode = bool(config.get("json_mode", False))
        self.top_p = float(config.get("top_p", 1.0) or 1.0)
        self.retries = int(config.get("retries", 3) or 3)
        self.extra_headers = config.get("extra_headers") or {}
        # 思考模式（DeepSeek V4 / V4.1 系列等支持）：开启后用推理模式，
        # 官方默认 reasoning_effort=high；关闭时显式发 thinking=disabled，
        # 防止模型默认带思考、把推理过程当正文返回。
        self.thinking = bool(config.get("thinking", False))
        self.reasoning_effort = str(config.get("reasoning_effort") or "high")
        self.disable_thinking = bool(config.get("disable_thinking", True))

    # -- 对外信息 ---------------------------------------------------------
    @property
    def label(self):
        if self.model:
            return "%s · %s" % (self.name, self.model)
        return self.name

    def endpoint(self):
        base = self.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return base + "/chat/completions"

    # -- 核心调用 ---------------------------------------------------------
    def chat(self, system_prompt, user_prompt, expect_json=False):
        """返回 (文本, 用量dict)。失败抛 LLMError。"""
        if not self.base_url:
            raise LLMError("席位「%s」未配置 API 地址" % self.key)
        if not self.model:
            raise LLMError("席位「%s」未配置模型名" % self.key)

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})

        body = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if self.top_p and abs(self.top_p - 1.0) > 1e-6:
            body["top_p"] = self.top_p
        if expect_json and self.json_mode:
            body["response_format"] = {"type": "json_object"}
        if self.thinking:
            # DeepSeek V4 / V4.1：思考模式靠参数开启（不是靠换模型名）
            body["reasoning_effort"] = self.reasoning_effort or "high"
            body["thinking"] = {"type": "enabled"}
        elif self.disable_thinking:
            # 显式关闭思考：避免个别模型（如 V4.1-Flash）默认带思考、
            # 把思考过程当正文返回，导致台词里混进英文推理
            body["thinking"] = {"type": "disabled"}

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "AI-Werewolf/1.0",
        }
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        for k, v in self.extra_headers.items():
            if k:
                headers[str(k)] = str(v)
        # OpenRouter 建议携带的标识头（不带也无妨）
        if "openrouter.ai" in self.base_url:
            headers.setdefault("HTTP-Referer", "https://localhost/ai-werewolf")
            headers.setdefault("X-Title", "AI Werewolf")

        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        last_err = None

        for attempt in range(1, self.retries + 1):
            req = urllib.request.Request(self.endpoint(), data=payload,
                                         headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                data = json.loads(raw)
                text, usage = self._parse_response(data, messages)
                self.meter.add(self.key, usage["prompt"], usage["completion"],
                               usage["estimated"])
                return text, usage
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode("utf-8", "replace")[:400]
                except Exception:
                    pass
                last_err = "HTTP %s %s %s" % (e.code, e.reason, detail)
                # 4xx（除 429）基本是配置问题，重试无意义
                if 400 <= e.code < 500 and e.code != 429:
                    break
            except urllib.error.URLError as e:
                last_err = "网络错误：%s" % (e.reason,)
            except Exception as e:                       # 超时 / JSON 解析等
                last_err = "%s: %s" % (type(e).__name__, e)
            if attempt < self.retries:
                time.sleep(min(1.5 * attempt + random.random(), 5.0))

        self.meter.add_error(self.key)
        raise LLMError("席位「%s」调用失败：%s" % (self.key, last_err))

    # -- 响应解析 ---------------------------------------------------------
    def _parse_response(self, data, messages):
        if isinstance(data, dict) and data.get("error"):
            raise LLMError(str(data["error"])[:300])

        text = ""
        try:
            choices = data.get("choices") or []
            if choices:
                msg = choices[0].get("message") or {}
                content = msg.get("content")
                if isinstance(content, list):          # 某些网关返回分段内容
                    parts = []
                    for seg in content:
                        if isinstance(seg, dict):
                            parts.append(seg.get("text") or seg.get("content") or "")
                        else:
                            parts.append(str(seg))
                    content = "".join(parts)
                text = content or ""
                if not text:
                    # 少数模型把内容放在 reasoning 字段
                    text = msg.get("reasoning_content") or msg.get("reasoning") or ""
        except Exception:
            text = ""
        text = (text or "").strip()

        usage_raw = data.get("usage") or {}
        p = usage_raw.get("prompt_tokens") or usage_raw.get("input_tokens")
        c = usage_raw.get("completion_tokens") or usage_raw.get("output_tokens")
        # 思考模式的推理 token 也要计入消耗（官方通常已含在 completion_tokens 内，
        # 若单独给出 reasoning_tokens 则补上差额，避免低估账单）
        rt = (usage_raw.get("completion_tokens_details") or {}).get("reasoning_tokens") \
            or usage_raw.get("reasoning_tokens")
        if self.thinking and rt and c is not None and int(rt) > int(c):
            # 仅思考模式才做补差：关闭思考时服务端的 completion_tokens 已是准确值
            c = int(rt)
        estimated = False
        if p is None or c is None:
            estimated = True
            p = sum(estimate_tokens(m["content"]) for m in messages)
            c = estimate_tokens(text)
        return text, {"prompt": int(p), "completion": int(c), "estimated": estimated,
                      "reasoning": int(rt or 0)}


# ----------------------------------------------------------------------------
# 便捷：一次性的连通性测试
# ----------------------------------------------------------------------------
def test_connection(config, meter=None):
    """用一条极短的请求验证 API 是否可用，返回 (ok, 提示文本)。"""
    meter = meter or TokenMeter()
    agent = LLMAgent(config.get("name") or "测试", config, meter)
    try:
        text, usage = agent.chat(
            "你是一个测试助手，只回复一个 JSON。",
            '请只回复：{"ok": true}',
            expect_json=True,
        )
        return True, "连通成功 ✓ 回复：%s（本次 %d tokens）" % (
            (text or "(空)")[:80], usage["prompt"] + usage["completion"])
    except LLMError as e:
        return False, str(e)
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)
