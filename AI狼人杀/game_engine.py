# -*- coding: utf-8 -*-
"""
game_engine.py —— AI 狼人杀 自动博弈引擎（v2）
==============================================
* 人数 6~12 人自选，身份开局前自选（自定义模式）或随机（经典模式）。
* 身份池：狼人 / 狼王 / 白狼王（狼人阵营）；平民 / 预言家 / 女巫 / 猎人 / 守卫 / 白痴（好人阵营）。
* 三名信息层严格隔离：
    - 公开层（public）：所有玩家都能看到（公开发言、死讯、投票、上帝播报）
    - 观众层（godview）：只有上帝/观众能看（狼队刀口、守卫选择、结算判定、私密耳语）
    - 狼队层（wolfchat）：只有狼人阵营和观众能看（夜间密谈）
  好人玩家的提示词里永远不会出现"观众层"和"狼队层"的内容。
* 9 项游戏设置（默认关闭，遗言默认开启）见 DEFAULT_SETTINGS。
* 引擎只负责"逻辑 + 提示词 + 事件回调"，不含任何界面代码，通过 emit(kind, payload) 抛事件。
"""

import json
import random
import re
import threading
import time

from llm_client import LLMAgent, LLMError, extract_json, pick_seat

# ---------------------------------------------------------------------------
# 身份定义
# ---------------------------------------------------------------------------
ROLE_WOLF = "狼人"
ROLE_WOLF_KING = "狼王"
ROLE_WHITE_WOLF_KING = "白狼王"
ROLE_VILLAGER = "平民"
ROLE_SEER = "预言家"
ROLE_WITCH = "女巫"
ROLE_HUNTER = "猎人"
ROLE_GUARD = "守卫"
ROLE_IDIOT = "白痴"

CAMP_WOLF = "狼人阵营"
CAMP_GOOD = "好人阵营"

WOLF_ROLES = (ROLE_WOLF, ROLE_WOLF_KING, ROLE_WHITE_WOLF_KING)
GOD_ROLES = (ROLE_SEER, ROLE_WITCH, ROLE_HUNTER, ROLE_GUARD, ROLE_IDIOT)
# 身份池：狼人 / 平民 可重复，其余每个最多 1 个
CUSTOM_ROLE_POOL = [ROLE_WOLF, ROLE_VILLAGER, ROLE_GUARD, ROLE_HUNTER, ROLE_WITCH,
                    ROLE_SEER, ROLE_IDIOT, ROLE_WOLF_KING, ROLE_WHITE_WOLF_KING]
UNIQUE_ROLES = [r for r in CUSTOM_ROLE_POOL if r not in (ROLE_WOLF, ROLE_VILLAGER)]
MAX_GODS = len(GOD_ROLES)          # 最多 5 个神职（守卫/猎人/女巫/预言家/白痴）

MIN_PLAYERS, MAX_PLAYERS = 6, 12

# 经典模式默认牌组（11 人）
ROLE_DECK_CLASSIC = ([ROLE_WOLF] * 4 + [ROLE_VILLAGER] * 4
                     + [ROLE_SEER, ROLE_WITCH, ROLE_HUNTER])

# 一句话技能说明（给"规则速查"用，写得极简）
ROLE_BRIEF = {
    ROLE_WOLF: "每晚与队友共同刀一人；白天可自爆。",
    ROLE_WOLF_KING: "每晚刀人；被刀杀或被放逐出局时可开枪带走一人（被毒杀不能）；"
                    "白天可自爆，自爆不触发开枪。",
    ROLE_WHITE_WOLF_KING: "每晚刀人；白天自爆时可带走任意一名玩家，随后直接进入黑夜。",
    ROLE_VILLAGER: "没有技能，靠发言与投票。",
    ROLE_SEER: "每晚查验一人是狼人还是好人，结论只有你知道。",
    ROLE_WITCH: "有一瓶解药（救回当晚被刀者）和一瓶毒药（毒死一人）。",
    ROLE_HUNTER: "被刀杀或被放逐出局时可开枪带走一人（被毒杀不能）。",
    ROLE_GUARD: "每晚守护一人免于被狼刀；不能连续两晚守同一人，也不能连续两晚空守。",
    ROLE_IDIOT: "被投票放逐时翻牌不死，但从此失去投票权（仍可发言）。",
}

# 经典模式固定说明
CLASSIC_BRIEF = ("4 名狼人、4 名平民、预言家、女巫、猎人各 1 名。"
                 "狼人每晚共同刀一人；预言家每晚验一人；女巫一瓶解药一瓶毒药；"
                 "猎人被刀杀或被放逐时可开枪。")

# ---------------------------------------------------------------------------
# 9 项游戏设置（默认关闭；遗言默认开启）
# ---------------------------------------------------------------------------
DEFAULT_SETTINGS = {
    "wolf_explode": True,       # 普通狼人白天是否可自爆
    "last_words": True,         # 是否启用遗言机制
    "guard_double": True,       # 是否启用奶穿（同守同救死亡）
    "wolf_win_mode": "side",    # side=屠边（民或神全灭即胜） / all=屠城（好人全灭才胜）
    "reveal_death": False,      # 死亡是否公开身份与死因（唯一默认关闭项）
    "witch_know_kill": True,    # 女巫是否每晚都知道被刀的人
    "witch_two_potions": True,  # 女巫是否同一晚可用两瓶药
    "sheriff": True,            # 是否启用警长机制
    "allow_abstain": True,      # 放逐投票 / 警长投票是否允许弃票
    "context_lines": 60,        # 拼接进提示词的最近发言条数（20/30/60）
}


def normalize_settings(raw):
    s = dict(DEFAULT_SETTINGS)
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k in s:
                s[k] = v
    s["wolf_win_mode"] = "all" if s.get("wolf_win_mode") == "all" else "side"
    try:
        s["context_lines"] = int(s.get("context_lines") or 60)
    except (TypeError, ValueError):
        s["context_lines"] = 60
    if s["context_lines"] not in (20, 30, 60):
        s["context_lines"] = 60
    for k in ("wolf_explode", "last_words", "guard_double", "reveal_death",
              "witch_know_kill", "witch_two_potions", "sheriff", "allow_abstain"):
        s[k] = bool(s.get(k))
    return s


# ---------------------------------------------------------------------------
# 身份校验
# ---------------------------------------------------------------------------
def validate_roles(roles):
    """校验自定义身份配置，返回 (是否合法, 错误信息, 统计信息)。"""
    n = len(roles or [])
    if n < MIN_PLAYERS or n > MAX_PLAYERS:
        return False, "人数必须在 %d~%d 人之间（当前 %d 人）" % (MIN_PLAYERS, MAX_PLAYERS, n), {}
    dup = {}
    for r in roles:
        if r not in CUSTOM_ROLE_POOL:
            return False, "出现不支持的身份：%s" % r, {}
        dup[r] = dup.get(r, 0) + 1
    for r, c in dup.items():
        if r in UNIQUE_ROLES and c > 1:
            return False, "身份「%s」不可重复（当前选了 %d 个）" % (r, c), {}
    wolves = [r for r in roles if r in WOLF_ROLES]
    gods = [r for r in roles if r in GOD_ROLES]
    villagers = [r for r in roles if r == ROLE_VILLAGER]
    if not wolves:
        return False, "至少要有一名狼人阵营的角色", {}
    if not gods and not villagers:
        return False, "好人阵营不能为空，请至少配置一名神职或平民", {}
    if len(gods) > MAX_GODS:
        return False, "神职最多 %d 个" % MAX_GODS, {}
    stat = {"wolves": len(wolves), "gods": len(gods), "villagers": len(villagers),
            "players": n, "wolf_roles": wolves, "god_roles": gods}
    return True, "", stat


# 向后兼容旧名字
validate_custom_roles = validate_roles


def balance_hint(stat):
    if not stat:
        return ""
    w, g, v = stat["wolves"], stat["gods"], stat["villagers"]
    good = g + v
    tips = []
    if w >= good:
        tips.append("狼人已不少于好人，开局就接近狼人必胜")
    elif w * 2 > good + 1:
        tips.append("狼人偏多，狼人优势")
    elif w * 2 + 2 <= good:
        tips.append("狼人偏少，好人优势")
    if g >= 4:
        tips.append("神职 %d 个，技能资源充裕" % g)
    if v == 0:
        tips.append("没有平民，胜负只按神职/狼人判定")
    if w == 1:
        tips.append("只有 1 只狼，很难赢")
    if not tips:
        tips.append("阵容比较均衡")
    return "；".join(tips)


def seat_name(n):
    return "%d号" % n


def list_names(seats):
    return "、".join(seat_name(n) for n in sorted(seats))


def _fmt_weight(v):
    return str(int(v)) if abs(v - int(v)) < 1e-9 else ("%.1f" % v)


class StopGame(Exception):
    """收到停止指令时抛出，用于干净地中断对局。"""


class Player(object):
    def __init__(self, seat, role, agent):
        self.seat = seat
        self.role = role
        self.agent = agent
        self.alive = True
        self.sheriff = False
        self.death_cause = ""
        self.death_round = 0
        self.investigated = {}          # 预言家私有：{seat: 是否狼人}
        self.tokens_at_death = None
        self.speech_count = 0
        self.revealed = False           # 白痴翻牌
        self.can_vote = True
        self.has_shot = False           # 猎人 / 狼王是否已经开过枪（防连锁死循环）

    @property
    def name(self):
        return seat_name(self.seat)

    @property
    def camp(self):
        return CAMP_WOLF if self.is_wolf else CAMP_GOOD

    @property
    def is_wolf(self):
        return self.role in WOLF_ROLES

    @property
    def is_god(self):
        return self.role in GOD_ROLES


# ===========================================================================
class WerewolfGame(object):
    """一局 AI 狼人杀（人数 6~12 可配）。"""

    def __init__(self, seat_configs, judge_config, emit, token_meter,
                 speed=1.4, seed=None, roles=None, settings=None):
        self.emit = emit
        self.meter = token_meter
        self.speed = max(0.0, float(speed))
        self.paused = False
        self.random = random.Random(seed)
        self.stop_flag = threading.Event()
        self.settings = normalize_settings(settings)
        self.round_no = 0
        self.phase = "准备"
        self.winner = None
        self.win_reason = ""
        self.sheriff_seat = None
        self.sheriff_done = False

        self.chat_lines = []            # 公开信息流水（给玩家提示词用）
        self.wolf_lines = []            # 狼队频道流水（只给狼人）
        self.history = []               # 全部事件（导出战报用）
        self.chat_records = []          # 观众看得到的记录（导出用）
        self.day_public = []            # 当天公开事实
        self.death_order = []           # 出局顺序
        self.pending_last_words = []    # 待发表遗言（(seat, 死亡顺序)）
        self.guard_history = []
        self.potions = {"antidote": True, "poison": True, "saved_this_night": False}
        self.wolves = []
        self.self_exploded = False
        self.explode_asked = set()      # 当天已经问过"是否自爆"的狼（每狼每天只问一次）
        self.vote_detail = {}
        self._explode_take = None
        self._explode_speech = ""

        # 建席位
        self.custom_roles = list(roles) if roles else None
        if self.custom_roles:
            self.seat_count = len(self.custom_roles)
        else:
            self.seat_count = len(ROLE_DECK_CLASSIC)
        self.players = {}
        for seat in range(1, self.seat_count + 1):
            cfg = seat_configs.get(seat) or {}
            role = self.custom_roles[seat - 1] if self.custom_roles else None
            self.players[seat] = Player(seat, role,
                                        LLMAgent(seat_name(seat), cfg, token_meter))
        self.judge = LLMAgent("上帝", judge_config, token_meter)

    # ------------------------------------------------------------------
    # 基础
    # ------------------------------------------------------------------
    @property
    def seats(self):
        return list(range(1, self.seat_count + 1))

    def _check_stop(self):
        if self.stop_flag.is_set():
            raise StopGame()

    def _sleep(self, factor=1.0):
        while self.paused:
            self._check_stop()
            time.sleep(0.1)
        total = max(0.04, 0.2 + 0.25 * self.speed * factor)
        waited = 0.0
        while waited < total:
            self._check_stop()
            if self.paused:
                continue
            time.sleep(0.04)
            waited += 0.04
        self._check_stop()

    # ------------------------------------------------------------------
    # 事件输出（三层信息流）
    # ------------------------------------------------------------------
    def public(self, text, msg="系统", seat=None, detail="", share=False):
        """公开层：所有玩家 + 观众可见。

        share=True 表示这段内容会进入 AI 的"公开发言实录"（只有玩家真的当众说过的
        话才该 share=True：发言 / 遗言 / 上警 / 表态 / 竞选演说 / 翻牌 / 自爆）。
        行动内幕、系统死讯、上帝播报只给观众看，不进 AI 上下文。
        """
        self._push(text, msg=msg, seat=seat, detail=detail, layer="public",
                   share=share)

    def godview(self, text, msg="上帝视角", detail=""):
        """观众层：只有上帝/观众可见，绝不进玩家提示词。"""
        self._push(text, msg=msg, seat=None, detail=detail, layer="god",
                   share=False)

    def wolfchat(self, seat, text, detail=""):
        """狼队层：狼人阵营 + 观众可见，好人不可见（不进公开发言流水）。"""
        payload = {"kind": "chat", "seat": seat, "msg": "狼队频道", "text": text,
                   "detail": detail, "round": self.round_no, "phase": self.phase,
                   "layer": "wolf", "share": False}
        self.history.append(payload)
        self.chat_records.append(payload)
        self.wolf_lines.append({"seat": seat, "text": text})
        self.emit("chat", payload)

    def _push(self, text, msg, seat, detail, layer, share=False):
        payload = {"kind": "chat", "seat": seat, "msg": msg, "text": text,
                   "detail": detail, "round": self.round_no, "phase": self.phase,
                   "layer": layer, "share": bool(share)}
        self.history.append(payload)
        self.chat_records.append(payload)
        if layer == "public" and share:
            self.chat_lines.append({"seat": seat, "text": text, "layer": "public"})
        self.emit("chat", payload)

    def speech(self, player, text, kind="发言", extra=""):
        text = self._clean(text)
        player.speech_count += 1
        self._push(text, msg=kind, seat=player.seat, detail=extra, layer="public",
                   share=True)
        return text

    def action(self, player, text):
        """夜间 / 技能行动的过程说明：观众可见，但**不进**其他 AI 的上下文。"""
        self._push(self._clean(text), msg="行动", seat=player.seat, detail="",
                   layer="public", share=False)

    @staticmethod
    def _clean(text):
        if not text:
            return ""
        t = str(text).strip()
        t = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", t).strip()
        t = t.strip("\"'“”")
        t = re.sub(r"\s*\n+\s*", " ", t)
        t = re.sub(r"\*\*|##+", "", t)
        if len(t) > 600:
            t = t[:600] + "…"
        return t.strip()

    def system_note(self, text):
        """系统提示（仅观众）：不进 AI 上下文。"""
        self._push(text, msg="系统", seat=None, detail="", layer="public",
                   share=False)

    # ------------------------------------------------------------------
    # 状态描述
    # ------------------------------------------------------------------
    def _board_text(self):
        parts = []
        for seat in self.seats:
            p = self.players[seat]
            if p.alive:
                mark = "存活"
                if p.revealed:
                    mark = "存活(已翻牌)"
                if p.sheriff:
                    mark = mark + "·警长"
            else:
                mark = "已出局"
                if self.settings["reveal_death"]:
                    mark = "已出局·%s·%s" % (p.role, p.death_cause or "出局")
            parts.append("%s%s" % (p.name, mark))
        return "｜".join(parts)

    def role_count_text(self):
        counts = {}
        for p in self.players.values():
            counts[p.role] = counts.get(p.role, 0) + 1
        return "、".join("%d个%s" % (n, r) for r, n in counts.items())

    def public_state_lines(self):
        """公开信息（进所有玩家的提示词，绝不含观众层/狼队层内容）。"""
        lines = []
        lines.append("当前阶段：第%d轮 · %s" % (self.round_no, self.phase))
        lines.append("本局共 %d 名玩家，身份构成：%s" % (self.seat_count, self.role_count_text()))
        lines.append("席位状态：" + self._board_text())
        lines.append("存活玩家：%s" % list_names(self.alive_seats()))
        if self.settings["sheriff"] and self.sheriff_seat:
            lines.append("警长：%s" % seat_name(self.sheriff_seat))
        if self.day_public:
            lines.append("本轮已公开的事实：")
            for item in self.day_public[-8:]:
                lines.append("  · " + item)
        return lines

    def private_state_lines(self, player):
        lines = []
        if player.is_wolf:
            mates = []
            for s in self.wolves:
                if s == player.seat:
                    continue
                mate = self.players[s]
                mates.append("%s（%s）" % (seat_name(s), mate.role))
            lines.append("你的狼队友：%s" % ("、".join(mates) if mates else "无（只剩你）"))
        if player.role == ROLE_SEER and player.investigated:
            got = ["%s → %s" % (seat_name(s), "狼人" if w else "好人")
                   for s, w in sorted(player.investigated.items())]
            lines.append("你验过的结果（只有你知道）：%s" % "；".join(got))
        if player.role == ROLE_WITCH:
            lines.append("你的药剂：解药%s，毒药%s"
                         % ("还在" if self.potions["antidote"] else "已用完",
                            "还在" if self.potions["poison"] else "已用完"))
        if player.role == ROLE_GUARD and self.guard_history:
            prev = self.guard_history[-1]
            lines.append("你上一晚的选择：%s"
                         % ("空守" if prev == "skip" else seat_name(prev)))
        return lines

    def wolf_context(self, limit=None):
        """狼队频道最近发言（只给狼人）。"""
        n = limit or max(8, int(self.settings["context_lines"]) // 3)
        items = self.wolf_lines[-n:]
        if not items:
            return "（狼队频道暂无发言）"
        return "\n".join("[%s] %s" % (seat_name(i["seat"]), i["text"]) for i in items)

    def public_context(self, limit=None):
        """AI 能看到的"公开发言实录"：只包含玩家当众说过的话。

        行动内幕（女巫用药、守卫守人、验人思路）、系统死讯、上帝播报都不在这里，
        它们只进观众台账。这样玩家只能靠别人"说出来的话"和投票行为推理。
        """
        n = limit or self.settings["context_lines"]
        items = self.chat_lines[-n:]
        if not items:
            return "（还没有人发言）"
        out = []
        for c in items:
            if c.get("seat") is None:
                out.append("[上帝] %s" % c["text"])
            else:
                out.append("[%s] %s" % (seat_name(c["seat"]), c["text"]))
        return "\n".join(out)

    def role_player(self, role):
        for p in self.players.values():
            if p.role == role:
                return p
        return None

    def alive_seats(self, camp=None, role=None, exclude=()):
        out = []
        for seat in self.seats:
            p = self.players[seat]
            if not p.alive or seat in exclude:
                continue
            if camp and p.camp != camp:
                continue
            if role and p.role != role:
                continue
            out.append(seat)
        return out

    def alive_voters(self, exclude=()):
        return [s for s in self.alive_seats(exclude=exclude) if self.players[s].can_vote]

    def living_roles(self):
        return set(p.role for p in self.players.values())

    # ------------------------------------------------------------------
    # 提示词（三层：身份卡 / 规则速查 / 阶段指引）
    # ------------------------------------------------------------------
    def _system_prompt(self, player):
        if self.custom_roles:
            brief = "\n".join("· %s：%s" % (r, ROLE_BRIEF[r]) for r in CUSTOM_ROLE_POOL)
        else:
            brief = "· " + CLASSIC_BRIEF + "\n· " + "\n· ".join(
                "%s：%s" % (r, ROLE_BRIEF[r]) for r in
                (ROLE_SEER, ROLE_WITCH, ROLE_HUNTER))
        return (
            "你是一局 AI 狼人杀里的玩家，席位 %s。像真人玩家一样思考、伪装、拉票、"
            "悍跳、倒钩都可以。\n\n"
            "【你的身份卡】\n"
            "身份：%s ｜ 阵营：%s\n"
            "技能：%s\n"
            "胜利条件：让自己所在的阵营获胜（你不必活着）。\n\n"
            "【规则速查】（常识，不必复述）\n%s\n"
            "· 发言按顺序进行，得票最高者被放逐，平票重投一轮，再平票则本轮无人出局。\n"
            "· 狼人可在自己发言结束后选择自爆：自爆者出局，当天剩余流程取消，直接进入黑夜。\n"
            "%s\n"
            "【行为要求】\n"
            "1. 结合场上具体席位号、死讯与票型说话，不要空话。\n"
            "2. 你只知道自己的身份和自己的私有信息；不要假装知道上帝才知道的事"
            "（除非你在撒谎骗人）。\n"
            "3. 中文口语，简洁、有逻辑，单次发言 40~150 字。\n"
            "4. 只输出要求的 JSON，不要代码块、不要多余文字。"
            % (player.name, player.role, player.camp,
               ROLE_BRIEF.get(player.role, ""), brief,
               ("· 警长票按 1.5 票计算，并决定发言顺序。"
                if self.settings["sheriff"] else ""))
        )

    def _judge_system_prompt(self):
        return (
            "你是一档 AI 狼人杀节目里的主持人兼裁判，风格沉稳有仪式感、简洁有力。\n"
            "【硬性要求】\n"
            "1. 每次只说你此刻被要求的那一句话，1~3 句，不超过 60 字。\n"
            "2. 只能使用【本轮事实】里给出的内容；不得自行补充死讯、身份或结论，"
            "不得复述上一个阶段的台词。\n"
            "3. 中文，不要引号包裹，不要 JSON，不要解释。"
        )

    def _user_prompt(self, player, task, fmt=None, extra=None):
        lines = []
        lines.append("【你的私有信息】")
        priv = self.private_state_lines(player)
        lines.extend(priv if priv else ["（无）"])
        lines.append("")
        lines.extend(self.public_state_lines())
        if player.is_wolf:
            lines.append("")
            lines.append("【狼队频道（仅狼人可见）】")
            lines.append(self.wolf_context())
        lines.append("")
        lines.append("【最近发言实录】")
        lines.append(self.public_context())
        lines.append("")
        if extra:
            lines.extend(extra)
            lines.append("")
        lines.append("【你的任务】" + task)
        if fmt:
            lines.append("【输出格式】只输出一个 JSON 对象：" + fmt)
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 调用
    # ------------------------------------------------------------------
    def call_judge(self, key, fact, fallback=None):
        """裁判播报：只依据 fact 里的事实，不喂玩家聊天记录。"""
        self._check_stop()
        lines = ["【本轮事实】%s" % (fact or "（无）"),
                 "场上：%s" % self._board_text(),
                 "现在正处于：第%d轮 · %s" % (self.round_no, self.phase),
                 "【你要做的事】%s" % self._judge_task(key),
                 "请只输出这一句主持词。"]
        try:
            text, _ = self.judge.chat(self._judge_system_prompt(), "\n".join(lines))
            self._after_call("上帝")
            # 裁判台词同样按句过滤，防止模型把英文推理混进来
            text = self.keep_chinese_lines(text) or self._clean(text)
            if not text or self._cjk_count(text) < 4:
                raise LLMError("空回复")
            self.godview_jugde(text)
            return text
        except LLMError as e:
            self.emit("error", {"text": str(e)})
            text = fallback or self._judge_fallback(key, fact)
            self.godview_jugde(text)
            return text

    def godview_jugde(self, text):
        """裁判台词：公开层（所有玩家都听到流程播报）+ 观众层样式。"""
        self._push(text, msg="上帝", seat=None, detail="", layer="public")

    @staticmethod
    def _judge_task(key):
        return {
            "intro": "宣布对局开始，并说明本局人数与身份构成（不要逐个介绍玩家）。",
            "reset": "宣布天黑请闭眼。",
            "wolf": "提示狼人睁眼商量目标。",
            "guard": "提示守卫睁眼选择守护对象。",
            "seer": "提示预言家睁眼查验。",
            "witch": "提示女巫睁眼用药。",
            "dawn": "公布昨夜死讯（严格只报给出的死讯）。",
            "day": "宣布白天讨论开始。",
            "explode": "宣布有人自爆，当天剩余流程取消，直接进入黑夜。",
            "hunter": "宣布出局者开枪。",
            "idiot": "宣布被放逐者翻牌为白痴，放逐无效。",
            "vote": "宣布开始投票。",
            "vote_result": "宣布计票结果。",
            "lastwords": "请出局者留下遗言。",
            "win": "宣布胜负与结束语。",
        }.get(key, "用一句话推进流程。")

    @staticmethod
    def _judge_fallback(key, fact):
        return {
            "intro": "对局开始，请各位准备。",
            "reset": "天黑请闭眼。",
            "wolf": "狼人请睁眼。",
            "guard": "守卫请睁眼，请选择你要守护的人。",
            "seer": "预言家请睁眼。",
            "witch": "女巫请睁眼。",
            "dawn": fact or "天亮了。",
            "day": "请各位开始讨论。",
            "explode": fact or "有人自爆，直接进入黑夜。",
            "hunter": "请开枪。",
            "idiot": fact or "放逐无效。",
            "vote": "请投票。",
            "vote_result": fact or "计票结束。",
            "lastwords": "请留下遗言。",
            "win": fact or "本局结束。",
        }.get(key, fact or "请继续。")

    # 思考残留 / 英文元叙述的识别（V4.1-Flash 等模型会把思考过程当正文返回）
    _META_PAT = re.compile(
        r"(?im)^\s*(let me|i need|i should|i want|the user|thinking|analysis|"
        r"let's|first,|okay,|actually|standby|process|draft|note:)\b")
    _CJK = re.compile(r"[\u4e00-\u9fff]")

    @staticmethod
    def _cjk_count(text):
        return len(re.findall(r"[\u4e00-\u9fff]", text or ""))

    def _looks_like_thinking(self, text):
        """判断一段文本像不像"思考过程 / 元叙述"而不是台词。"""
        if not text:
            return True
        cjk = self._cjk_count(text)
        low = text.lower()
        if ("the user" in low and "witch" in low) or "thinking process" in low:
            return True
        meta_lines = len(self._META_PAT.findall(text))
        eng = len(re.findall(r"[A-Za-z]", text))
        if cjk == 0 and eng > 40:
            return True
        if meta_lines >= 2 and eng > cjk * 2:
            return True
        if eng > 60 and cjk < 10:
            return True
        return False

    _SPLIT_PAT = re.compile(r"(?<=[。！？!?；;])")

    def keep_chinese_lines(self, text, min_cjk=6):
        """按句切分，只保留中文台词句。

        模型（尤其带思考的 V4.1-Flash）常把"英文推理 + 中文台词"混在同一行，
        所以这里逐句判断：中文太少的句子直接丢掉，只把中文句拼回来。
        """
        if not text:
            return ""
        keeps, seen = [], set()
        for raw_line in str(text).splitlines():
            for sent in self._SPLIT_PAT.split(raw_line):
                s = sent.strip().strip("`*-— ")
                if not s:
                    continue
                s = re.sub(r"^\s*(?:\d+[\.\)、]|[-*•])\s*", "", s)
                if self._cjk_count(s) < min_cjk:
                    continue
                if self._META_PAT.match(s):
                    continue
                if s in seen:
                    continue
                seen.add(s)
                keeps.append(s)
        out = ""
        for s in keeps:
            if not out:
                out = s
            elif out[-1] in "。！？!?；;，,":
                out += s
            else:
                out += "。" + s
        return self._clean(out)

    def extract_speech(self, text):
        """从模型输出里尽力取出"该说的话"；取不出就返回 None。

        * 优先取 JSON 的 speech 字段；
        * 否则逐句过滤，只保留中文台词，丢掉英文思考 / 元叙述；
        * 中文台词不足（整段其实是思考过程）时返回 None，由调用方走兜底台词。
        """
        if not text:
            return None
        data = extract_json(text)
        if isinstance(data, dict) and data.get("speech"):
            return self._clean(str(data["speech"]))
        m = re.search(r'"speech"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
        if m:
            try:
                val = json.loads('"%s"' % m.group(1))
            except Exception:
                val = m.group(1)
            val = self._clean(val)
            if val:
                return val
        cleaned = self.keep_chinese_lines(text)
        if self._cjk_count(cleaned) >= 8:
            return cleaned
        return None

    def ask_player(self, player, user, fmt_seats=None, fallback_speech=None,
                   fallback_action=None):
        """让某个 AI 玩家输出一段内容，返回 (json_or_None, 原始文本)。"""
        self._check_stop()
        if fmt_seats:
            user += "\n【可选目标】只能从这些席位中选择：%s" % list_names(fmt_seats)
        try:
            text, _ = player.agent.chat(self._system_prompt(player), user,
                                        expect_json=True)
            self._after_call(player.name)
            text = (text or "").strip()
        except LLMError as e:
            self.emit("error", {"text": str(e), "seat": player.seat})
            return None, (fallback_speech or "")

        data = extract_json(text)
        if data is None:
            # 抽不到 JSON：只接受"像台词"的中文内容，思考链一律丢弃
            speech = self.extract_speech(text)
            if speech:
                return {"speech": speech}, text
            if self._looks_like_thinking(text):
                self.godview("%s 这次输出的是思考过程（已改用兜底台词）：%s"
                             % (player.name, self._clean(text)[:120]))
            return None, (fallback_speech or "")
        return data, text

    def _after_call(self, key):
        self.emit("tokens", {
            "key": key, "total": self.meter.total,
            "prompt": self.meter.total_prompt, "completion": self.meter.total_completion,
            "calls": self.meter.total_calls, "errors": self.meter.total_errors,
            "per_agent": dict(self.meter.by_agent),
        })

    def _speech_of(self, data, raw, fallback=""):
        if isinstance(data, dict) and data.get("speech"):
            return self._clean(data["speech"])
        return self._clean(raw) or fallback

    def _pick(self, data, field, valid, fallback=None):
        """从模型输出里取一个合法席位号。"""
        if isinstance(data, dict):
            got = pick_seat(data.get(field), set(valid))
            if got is not None:
                return got
        if fallback is not None:
            return fallback
        return self.random.choice(list(valid)) if valid else None

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def run(self):
        try:
            self.setup()
            self.emit("phase", {"phase": "游戏开始", "round": 0})
            self.call_judge("intro", "本局共 %d 名玩家，身份构成：%s。"
                            % (self.seat_count, self.role_count_text()))
            while not self.winner:
                self._check_stop()
                self.round_no += 1
                self.night_phase()
                if self.check_win():
                    break
                self.day_phase()
                if self.check_win():
                    break
                if self.round_no >= 12:
                    self.winner = "平局"
                    self.win_reason = "超过 12 轮仍未分出胜负，本局作平。"
                    break
            if not self.winner:
                self.check_win()
            self._final_declare()
        except StopGame:
            self.system_note("对局已被手动停止。")
        except Exception as e:
            import traceback
            self.emit("error", {"text": "引擎异常：%s: %s" % (type(e).__name__, e),
                                "trace": traceback.format_exc()})
        finally:
            self.emit("game_over", {"winner": self.winner, "reason": self.win_reason,
                                    "tokens": self.meter.total,
                                    "calls": self.meter.total_calls})

    def setup(self):
        if self.custom_roles:
            for seat in self.seats:
                self.players[seat].role = self.custom_roles[seat - 1]
        else:
            deck = list(ROLE_DECK_CLASSIC)
            self.random.shuffle(deck)
            for seat in self.seats:
                self.players[seat].role = deck[seat - 1]
        self.wolves = [p.seat for p in self.players.values() if p.is_wolf]
        self.emit("seats", {"players": [self._seat_view(s) for s in self.seats]})
        self.emit("roles", {"roles": {s: self.players[s].role for s in self.seats}})

    def _seat_view(self, seat):
        p = self.players[seat]
        return {"seat": seat, "role": p.role, "alive": p.alive,
                "sheriff": p.sheriff, "agent": p.agent.label,
                "tokens": self.meter.agent_total(p.name),
                "death": p.death_cause, "speeches": p.speech_count,
                "revealed": p.revealed, "can_vote": p.can_vote}

    def push_seats(self):
        self.emit("seats", {"players": [self._seat_view(s) for s in self.seats]})

    # ------------------------------------------------------------------
    # 夜晚
    # ------------------------------------------------------------------
    def night_phase(self):
        self.phase = "夜晚"
        self.day_public = []
        self.potions["saved_this_night"] = False
        self.emit("phase", {"phase": "第%d夜" % self.round_no, "round": self.round_no,
                            "is_night": True})
        self.call_judge("reset", "第 %d 夜开始。" % self.round_no)
        self._sleep(0.2)

        wolf_target = self.wolf_round()
        guard_target, guard_skip = self.guard_round()
        self.seer_round()
        poison_target = self.witch_round(wolf_target)

        saved = bool(self.potions.get("saved_this_night"))
        guarded = bool(guard_target and wolf_target and guard_target == wolf_target
                       and not guard_skip)
        self.godview("狼队刀口：%s；守卫守护：%s；女巫用药：%s"
                     % (seat_name(wolf_target) if wolf_target else "无",
                        "空守" if guard_skip else (seat_name(guard_target)
                                                if guard_target else "未行动"),
                        ("解药救%s" % seat_name(wolf_target)) if saved else
                        (("毒药毒%s" % seat_name(poison_target)) if poison_target
                         else "未用药")))
        self._sleep(0.15)

        deaths = []
        if wolf_target and poison_target != wolf_target:
            if saved and guarded and self.settings["guard_double"]:
                deaths.append((wolf_target, "同守同救"))
                self.godview("判定：%s 同时被守卫守护与女巫解药救助，同守同救，依然出局。"
                             % seat_name(wolf_target))
            elif saved:
                pass
            elif guarded:
                self.godview("判定：%s 被守卫守住，昨夜存活。" % seat_name(wolf_target))
            else:
                deaths.append((wolf_target, "刀杀"))
        if poison_target:
            deaths.append((poison_target, "毒杀"))

        seen = {}
        for seat, cause in deaths:
            seen.setdefault(seat, cause)
        self._apply_night_deaths([(s, c) for s, c in seen.items()])

    def guard_round(self):
        guard = self.role_player(ROLE_GUARD)
        if not guard or not guard.alive:
            return None, False
        self.phase = "守卫守护"
        self.emit("phase", {"phase": "第%d夜 · 守卫守护" % self.round_no,
                            "round": self.round_no, "is_night": True})
        self.call_judge("guard", "守卫睁眼。")
        prev = self.guard_history[-1] if self.guard_history else None
        banned = [prev] if isinstance(prev, int) else []
        force_guard = (prev == "skip")
        can_skip = (prev is None) or isinstance(prev, int)
        candidates = [s for s in self.alive_seats(exclude=(guard.seat,))
                      if s not in banned]
        if not candidates:
            self.guard_history.append("skip")
            return None, True
        info = ["不能连续两晚守护同一人，也不能连续两晚空守。"]
        if force_guard:
            info.append("你上一晚空守，所以今晚必须守护一名玩家。")
        elif prev is None:
            info.append("这是第一晚，可以守护任意一人，也可以空守。")
        else:
            info.append("你上一晚守护了 %s，今晚必须换人（也可以空守）。" % seat_name(prev))
        info.append("可选守护对象：%s" % list_names(candidates))
        self.emit("turn", {"seat": guard.seat, "kind": "守护"})
        fmt = ('{"speech": "你的守护思路", "target": 席位号%s}'
               % ("" if force_guard else "或null"))
        user = self._user_prompt(
            guard, "请选择今晚要守护的玩家。%s" % ("必须守人，不能空守。" if force_guard
                                                 else "不想守就把 target 填 null。"),
            fmt=fmt, extra=info)
        data, raw = self.ask_player(guard, user, fmt_seats=candidates,
                                    fallback_speech="我看一下位置。")
        target = None if force_guard else pick_seat(
            (data or {}).get("target"), set(candidates))
        if target is None and force_guard:
            target = self.random.choice(candidates)
        skip = target is None
        self.guard_history.append("skip" if skip else target)
        self.action(guard, "%s（%s）" % (self._speech_of(data, raw, "…"),
                                        "空守" if skip else "守护 " + seat_name(target)))
        self.godview("守卫本夜选择：%s" % ("空守" if skip else seat_name(target)))
        self.emit("turn", {"seat": None})
        self._sleep(0.2)
        return (None if skip else target), skip

    def wolf_round(self):
        wolves_alive = [s for s in self.wolves if self.players[s].alive]
        if not wolves_alive:
            return None
        self.phase = "狼人行动"
        self.emit("phase", {"phase": "第%d夜 · 狼人行动" % self.round_no,
                            "round": self.round_no, "is_night": True})
        self.call_judge("wolf", "狼人睁眼商量目标。")
        candidates = self.alive_seats(exclude=tuple(wolves_alive))
        if not candidates:
            return None
        votes = {}
        for seat in wolves_alive:
            self._check_stop()
            p = self.players[seat]
            self.emit("turn", {"seat": seat, "kind": "狼人密谈"})
            user = self._user_prompt(
                p, "现在是狼人夜间密谈。请和队友商量今晚刀谁，并给出你自己的选择。",
                fmt='{"speech": "你在狼队频道里说的话", "target": 你要刀的席位号}',
                extra=["【狼队频道】存活狼人：%s" % list_names(wolves_alive),
                       "【可选刀口】%s" % list_names(candidates)])
            data, raw = self.ask_player(p, user, fmt_seats=candidates,
                                        fallback_speech="我建议刀 %s。"
                                        % seat_name(self.random.choice(candidates)))
            target = self._pick(data, "target", candidates)
            votes[seat] = target
            self.wolfchat(seat, self._speech_of(data, raw, "今晚刀 %s。"
                                                % seat_name(target)),
                          detail="锁定目标 → %s" % seat_name(target))
            self.emit("turn", {"seat": None})
            self._sleep(0.2)
        tally = {}
        for t in votes.values():
            tally[t] = tally.get(t, 0) + 1
        top = max(tally.values())
        target = self.random.choice(sorted([s for s, v in tally.items() if v == top]))
        self.godview("狼队投票：%s → 最终刀口 %s"
                     % ("，".join("%s→%s" % (seat_name(k), seat_name(v))
                                  for k, v in votes.items()), seat_name(target)))
        return target

    def seer_round(self):
        seer = self.role_player(ROLE_SEER)
        if not seer or not seer.alive:
            return None
        self.phase = "预言家验人"
        self.emit("phase", {"phase": "第%d夜 · 预言家验人" % self.round_no,
                            "round": self.round_no, "is_night": True})
        self.call_judge("seer", "预言家睁眼查验。")
        candidates = self.alive_seats(exclude=(seer.seat,) + tuple(seer.investigated.keys()))
        if not candidates:
            return None
        self.emit("turn", {"seat": seer.seat, "kind": "查验"})
        user = self._user_prompt(
            seer, "请选择今晚要查验的玩家，并说出你的思路（只有你自己知道）。",
            fmt='{"speech": "你的思路", "target": 要查验的席位号}',
            extra=["【可查验席位】%s" % list_names(candidates)])
        data, raw = self.ask_player(seer, user, fmt_seats=candidates,
                                    fallback_speech="我想再确认一个位置。")
        target = self._pick(data, "target", candidates)
        is_wolf = self.players[target].is_wolf          # ← 狼王/白狼王也算狼
        seer.investigated[target] = is_wolf
        self.action(seer, "%s（查验思路）" % self._speech_of(data, raw, "…"))
        self.godview("%s 查验：%s → %s" % (seer.name, seat_name(target),
                                          "狼人" if is_wolf else "好人"),
                     msg="上帝私语")
        self.emit("turn", {"seat": None})
        self._sleep(0.2)
        return target

    def witch_round(self, wolf_target):
        witch = self.role_player(ROLE_WITCH)
        if not witch or not witch.alive:
            return None
        self.phase = "女巫用药"
        self.emit("phase", {"phase": "第%d夜 · 女巫用药" % self.round_no,
                            "round": self.round_no, "is_night": True})
        self.call_judge("witch", "女巫睁眼用药。")
        pot = self.potions
        tell = bool(wolf_target) and (self.round_no == 1 or self.settings["witch_know_kill"])
        info = []
        if tell and pot["antidote"]:
            info.append("今晚 %s 被狼人刀了。" % seat_name(wolf_target))
        elif wolf_target and pot["antidote"]:
            info.append("今晚有人被刀，但你没有被告知是谁（规则如此）。")
        elif wolf_target:
            info.append("今晚有人被刀，你的解药已用完。")
        else:
            info.append("今晚狼人似乎没有出刀。")
        info.append("药剂：解药%s，毒药%s"
                    % ("还在" if pot["antidote"] else "已用完",
                       "还在" if pot["poison"] else "已用完"))
        if not self.settings["witch_two_potions"]:
            info.append("同一晚只能使用一瓶药（解药或毒药）。")
        poison_candidates = self.alive_seats(exclude=(witch.seat,
                                                     wolf_target if wolf_target else -1))
        if poison_candidates:
            info.append("可毒杀席位：%s" % list_names(poison_candidates))
        self.emit("turn", {"seat": witch.seat, "kind": "用药"})
        user = self._user_prompt(
            witch, "请决定今晚是否用药。use_antidote=true 表示用解药；"
                   "use_poison=true 并用 poison 指定目标表示下毒。",
            fmt='{"speech": "你的想法", "use_antidote": true/false, '
                '"use_poison": true/false, "poison": 席位号或null}',
            extra=["【女巫私有信息】" + "；".join(info)])
        data, raw = self.ask_player(witch, user, fallback_speech="我今晚先保留药剂。")
        data = data or {}
        use_antidote = bool(data.get("use_antidote")) and pot["antidote"] and bool(wolf_target)
        use_poison = bool(data.get("use_poison")) and pot["poison"] and bool(poison_candidates)
        poison_target = self._pick(data, "poison", poison_candidates) if use_poison else None
        if use_antidote and poison_target and not self.settings["witch_two_potions"]:
            poison_target = None
        if use_antidote:
            pot["antidote"] = False
            pot["saved_this_night"] = True
        if poison_target:
            pot["poison"] = False
        used = []
        if use_antidote:
            used.append("解药 → 救 %s" % seat_name(wolf_target))
        if poison_target:
            used.append("毒药 → 毒 %s" % seat_name(poison_target))
        self.action(witch, "%s（%s）" % (self._speech_of(data, raw, "…"),
                                        "；".join(used) if used else "今晚不用药"))
        self.godview("%s 用药：%s" % (witch.name, "；".join(used) if used else "不用药"),
                     msg="上帝私语")
        self.emit("turn", {"seat": None})
        self._sleep(0.2)
        return poison_target

    def _apply_night_deaths(self, deaths):
        """天亮结算：有死人只报一次死讯（含身份），零死亡才说平安夜。"""
        self.emit("phase", {"phase": "第%d天 · 天亮了" % self.round_no,
                            "round": self.round_no})
        if deaths:
            reveal = self.settings["reveal_death"]
            for seat, cause in deaths:
                self.kill(seat, cause)
                role = self.players[seat].role
                text = ("昨夜 %s 出局（%s）%s"
                        % (seat_name(seat), cause,
                           "，身份：%s" % role if reveal else ""))
                # 死讯是当众公布的事实 → share=True，进入 AI 的公开实录
                self.public(text, msg="系统", share=True)
                self.emit("death", {"seat": seat, "cause": cause, "reveal": reveal})
                self.godview("%s 出局，真实身份 %s，死因 %s"
                             % (seat_name(seat), role, cause))
            facts = "；".join("%s %s" % (seat_name(s), c) for s, c in deaths)
        else:
            facts = "平安夜，无人出局"
            self.public("昨夜是平安夜，无人出局。", msg="系统", share=True)
        self.call_judge("dawn", facts)
        # 遗言登记：只有第一晚的死亡（毒杀除外）才有遗言
        if self.settings["last_words"] and self.round_no == 1:
            for seat, cause in deaths:
                if cause != "毒杀":
                    self.pending_last_words.append((seat, self._death_index(seat)))
        self.day_public.append("第%d夜出局：%s" % (self.round_no, facts))
        self.push_seats()

    def _death_index(self, seat):
        try:
            return self.death_order.index(seat)
        except ValueError:
            return len(self.death_order)

    def kill(self, seat, cause, round_no=None):
        p = self.players[seat]
        if not p.alive:
            return
        p.alive = False
        p.death_cause = cause
        p.death_round = round_no or self.round_no
        p.tokens_at_death = self.meter.agent_total(p.name)
        self.death_order.append(seat)
        if p.sheriff:
            p.sheriff = False
            self.sheriff_seat = None
            self.godview("警长 %s 出局，警徽需要移交。" % p.name)
            self._transfer_badge(p)

    def _transfer_badge(self, dead):
        alive = self.alive_seats()
        if not alive:
            return
        self.emit("turn", {"seat": dead.seat, "kind": "移交警徽"})
        user = self._user_prompt(
            dead, "你出局了，请决定把警徽移交给哪位存活玩家；不想交就填 null（撕掉警徽）。",
            fmt='{"speech": "你的话", "target": 席位号或null}',
            extra=["【存活玩家】%s" % list_names(alive)])
        data, raw = self.ask_player(dead, user, fmt_seats=alive,
                                    fallback_speech="警徽交给 %s。" % seat_name(alive[0]))
        target = pick_seat((data or {}).get("target"), set(alive))
        if target:
            self.players[target].sheriff = True
            self.sheriff_seat = target
            self.public("%s 把警徽移交给了 %s。" % (dead.name, seat_name(target)), msg="系统")
        else:
            self.public("%s 撕掉了警徽。" % dead.name, msg="系统")
        self.emit("turn", {"seat": None})
        self.push_seats()

    # ------------------------------------------------------------------
    # 白天
    # ------------------------------------------------------------------
    def day_phase(self):
        self.phase = "白天"
        self.self_exploded = False
        self.explode_asked = set()      # 新的一天，重新允许每只狼判定一次自爆
        self.emit("phase", {"phase": "第%d天" % self.round_no, "round": self.round_no})
        # 夜间被刀/被同守同救者开枪（毒杀不触发）
        for seat in list(self.death_order):
            p = self.players[seat]
            if p.death_round == self.round_no and p.death_cause != "毒杀":
                self.maybe_shoot(seat)
        # 第一晚死者的遗言（白天最先说）
        if self.round_no == 1:
            self.flush_last_words(night=True)
        # 上警（第一个白天）
        if self.settings["sheriff"] and not self.sheriff_done:
            self.sheriff_election()
        if self.check_win():
            return
        self.call_judge("day", "第 %d 天讨论开始。" % self.round_no)
        self._sleep(0.2)
        self.speak_round()
        if self.check_win():
            return
        if not self.self_exploded:
            self.vote_round()
        else:
            self.godview("发生自爆，当天剩余流程取消，直接进入黑夜。")
        # 白天死者的遗言（整轮最后，按死亡顺序）
        self.flush_last_words(night=False)

    def speak_round(self):
        for seat in self.speaking_order():
            self._check_stop()
            p = self.players[seat]
            if not p.alive or self.self_exploded:
                continue
            # 狼人在自己发言前先决定是否自爆（符合"自爆即插队进黑夜"的直觉）
            if p.is_wolf and seat not in self.explode_asked:
                self.explode_asked.add(seat)
                if self._ask_explode(p):
                    self._do_explode(p)
                    return
            self.emit("turn", {"seat": seat, "kind": "发言"})
            user = self._user_prompt(
                p, "现在轮到你发言。请分析死讯、票型与各人态度，给出你的怀疑对象与立场。",
                fmt='{"speech": "你的发言（40~150字）", "suspect": 你最怀疑的席位号}')
            data, raw = self.ask_player(p, user, fallback_speech="我先听听后面的。")
            suspect = None
            if isinstance(data, dict):
                suspect = pick_seat(data.get("suspect"),
                                    set(self.alive_seats(exclude=(seat,))))
            self.speech(p, self._speech_of(data, raw, "过。"), kind="发言",
                        extra=("怀疑对象 → %s" % seat_name(suspect)) if suspect else "")
            self.emit("turn", {"seat": None})
            self._sleep(0.25)
            # 该玩家发言结束后，让还没表态过的狼判定一次是否自爆
            if self.explode_check(exclude=(seat,)):
                return

    def speaking_order(self):
        alive = self.alive_seats()
        if not alive:
            return []
        if self.settings["sheriff"] and self.sheriff_seat \
                and self.players[self.sheriff_seat].alive:
            idx = alive.index(self.sheriff_seat) if self.sheriff_seat in alive else 0
            return alive[idx:] + alive[:idx]
        return alive

    def explode_check(self, exclude=()):
        """每名玩家发言结束后判定一次自爆。

        关键约束：**同一天内每只狼只问一次**（`explode_asked`）。
        否则每有人发言就会把全部狼重新问一遍，既浪费 token，又会让模型反复
        "重新推理"从而输出过时情报（例如队友早已出局还在引用它）。
        """
        wolves = [s for s in self.alive_seats() if self.players[s].is_wolf]
        if not wolves:
            return False
        for seat in wolves:
            if seat in exclude:
                continue
            p = self.players[seat]
            if p.role == ROLE_WOLF and not self.settings["wolf_explode"]:
                continue
            if seat in self.explode_asked:
                continue
            self.explode_asked.add(seat)
            if self._ask_explode(p):
                self._do_explode(p)
                return True
        return False

    def _ask_explode(self, player):
        is_white = player.role == ROLE_WHITE_WOLF_KING
        mates = [s for s in self.wolves if self.players[s].alive and s != player.seat]
        info = ["现在轮到你判定是否自爆（这是主动技能，随时可用）：",
                "· 自爆后你立即出局，当天剩余发言与投票全部取消，直接进入黑夜；",
                "· 狼队少一个人，如果自爆后场上再没有狼人，好人阵营立即获胜；",
                "· 自爆的机会只有这一次，一旦填 true 就不会再有说话机会。",
                "当前存活狼队友：%s" % (list_names(mates) if mates else "无（只剩你）")]
        if player.role == ROLE_WOLF_KING:
            info.append("你是狼王：自爆不会触发你的开枪技能（只有被刀/被放逐才能开枪）。")
        if is_white:
            info.append("你是白狼王：自爆时可以带走任意一名存活玩家。")
        info.append("不打算自爆就把 explode 填 false，正常发言。")
        self.emit("turn", {"seat": player.seat, "kind": "自爆判定"})
        user = self._user_prompt(
            player, "请决定现在是否自爆（explode=true/false）。",
            fmt='{"explode": true/false, "speech": "你自爆时要说的话（不自爆则填空）", '
                '"take": 要带走的席位号或null}',
            extra=info)
        data, raw = self.ask_player(player, user, fallback_speech="")
        self.emit("turn", {"seat": None})
        if not isinstance(data, dict):
            return False
        want = data.get("explode")
        if isinstance(want, str):
            want = want.strip().lower() in ("true", "1", "yes", "是", "自爆")
        if want:
            self._explode_speech = self._speech_of(data, raw, "")
            self._explode_take = data.get("take")
        return bool(want)

    def _do_explode(self, player):
        self.emit("phase", {"phase": "第%d天 · 狼人自爆" % self.round_no,
                            "round": self.round_no})
        say = getattr(self, "_explode_speech", "") or "我自爆。"
        # 自爆宣言只发一条（公开层）。狼人自己也会在公开实录里看到，不再重复发到狼队频道
        self.public("%s【自爆】%s" % (player.name, say), msg="自爆",
                    seat=player.seat, share=True)
        self.public("%s 自爆出局。" % player.name, msg="系统", share=True)
        self.kill(player.seat, "自爆")
        self.emit("death", {"seat": player.seat, "cause": "自爆",
                            "reveal": self.settings["reveal_death"]})
        self.self_exploded = True
        self.push_seats()
        self.call_judge("explode", "%s（%s）自爆，直接进入黑夜。"
                        % (player.name, player.role))
        if player.role == ROLE_WHITE_WOLF_KING:
            alive_others = self.alive_seats()
            if alive_others:
                taken = pick_seat(getattr(self, "_explode_take", None), set(alive_others))
                if taken is None:
                    taken = self.random.choice(alive_others)
                self.public("%s（白狼王）带走了 %s。" % (player.name, seat_name(taken)),
                            msg="系统", share=True)
                self.kill(taken, "白狼王带走")
                self.emit("death", {"seat": taken, "cause": "白狼王带走",
                                    "reveal": self.settings["reveal_death"]})
                self.push_seats()
                self.maybe_shoot(taken)

    def _vote_of(self, data, targets, allow_abstain):
        """解析一次投票：返回 (目标席位 或 None, 是否弃票)。

        允许弃票时，模型可返回 null / "弃票" 表示弃票；未给出有效目标又不满足
        弃票条件时，才回退成随机一票（保证流程不卡）。
        """
        if allow_abstain and isinstance(data, dict):
            raw = data.get("vote")
            if raw is None:
                return None, True
            s = str(raw).strip()
            if s in ("", "null", "None", "弃票", "弃权", "abstain", "pass"):
                return None, True
        target = self._pick(data, "vote", targets, fallback=None)
        if target is None:
            target = self.random.choice(targets) if targets else None
        return target, False

    def vote_round(self):
        alive = self.alive_seats()
        candidates = [s for s in alive if not self.players[s].revealed]
        voters = self.alive_voters()
        if len(alive) <= 1 or not voters or len(candidates) <= 1:
            return None
        allow_abstain = bool(self.settings["allow_abstain"])
        self.phase = "投票"
        self.emit("phase", {"phase": "第%d天 · 投票" % self.round_no,
                            "round": self.round_no})
        self.call_judge("vote", "开始投票放逐。")
        votes, reasons = {}, {}
        for seat in voters:
            self._check_stop()
            p = self.players[seat]
            targets = [s for s in candidates if s != seat]
            if not targets:
                continue
            self.emit("turn", {"seat": seat, "kind": "投票"})
            task = "请投票决定本轮的放逐对象，并说明理由。"
            fmt = '{"vote": 你要投的席位号, "speech": "一句话理由"}'
            extra = ["【可投票席位】%s" % list_names(targets)]
            if allow_abstain:
                task += "本轮允许弃票：不想投就把 vote 填 null。"
                fmt = ('{"vote": 席位号或null（null=弃票）, "speech": "一句话理由"}')
                extra.append("你可以弃票（vote 填 null）。")
            user = self._user_prompt(p, task, fmt=fmt, extra=extra)
            data, raw = self.ask_player(p, user, fmt_seats=targets,
                                        fallback_speech="我投 %s。" % seat_name(targets[0]))
            target, abstained = self._vote_of(data, targets, allow_abstain)
            votes[seat] = target
            reasons[seat] = self._speech_of(
                data, raw, "我弃票。" if abstained else "票 %s。" % seat_name(target))
            self.emit("vote", {"voter": seat, "target": target,
                               "abstain": abstained,
                               "reason": reasons[seat], "round": self.round_no})
            self.godview("%s %s%s" % (seat_name(seat),
                                      "弃票" if abstained else
                                      ("投票 → " + seat_name(target)),
                                      "（警长票 1.5）" if (p.sheriff and not abstained)
                                      else ""))
            self.emit("turn", {"seat": None})
            self._sleep(0.1)
        self._settle_votes(votes, tag="放逐", voters=voters)
        self.push_seats()

    def _settle_votes(self, votes, tag="放逐", allow_revote=True, voters=None,
                      only_targets=None):
        """结算一轮投票。

        only_targets: 限定有效被投票人（重投时用来排除平票者，避免"投给平票者"的脏票
                      被计入）。
        """
        if not votes:
            return None
        tally = {}
        abstain = []
        for voter, target in votes.items():
            if not self.players[voter].alive:      # 投票者本轮已出局，其票作废
                continue
            if target is None:                     # 弃票
                abstain.append(voter)
                continue
            if target not in self.players:
                continue
            if only_targets is not None and target not in only_targets:
                continue                            # 重投时投给平票者 → 无效票
            if not self.players[target].alive and target not in self.death_order[-1:]:
                continue
            tally[target] = tally.get(target, 0.0) + (
                1.5 if self.players[voter].sheriff else 1.0)
        cast = len(votes) if voters is None else len(voters)
        # 一个玩家可能"自己弃票、同时被别人投"：这类人不能算进弃票名单，
        # 否则计票会自相矛盾（既写"A号 2票"又写"弃票：A号"）。
        abstain = [s for s in abstain if s not in tally]
        abs_text = ""
        if abstain:
            abs_text = "，弃票：%s" % list_names(abstain)
        if not tally:
            # 全员弃票（或全部票作废）→ 本轮无人出局
            self.public("计票：本轮所有人都弃票，无人出局。", msg="系统", share=True)
            self.call_judge("vote_result", "全员弃票，本轮无人出局。")
            self.emit("vote_result", {"tally": {}, "out": None, "tag": tag,
                                      "finalists": [], "abstain": abstain})
            return None
        detail = "，".join("%s %s 票" % (seat_name(s), _fmt_weight(tally[s]))
                           for s in sorted(tally, key=lambda x: -tally[x]))
        detail += abs_text
        top = max(tally.values())
        voters_n = len(votes) if voters is None else len(voters)
        # 平票 = 多个候选人的最高票相同；弃票不是"投给某人"，不与票数比较。
        finalists = sorted([s for s, v in tally.items() if abs(v - top) < 1e-9])
        if len(finalists) == 1:
            out = finalists[0]
            self.public("计票：%s → %s 出局。" % (detail, seat_name(out)), msg="系统",
                        share=True)
            self.call_judge("vote_result", "%s；%s 以 %s 票出局。"
                            % (detail, seat_name(out), _fmt_weight(top)))
            self.emit("vote_result", {"tally": tally, "out": out, "tag": tag,
                                      "finalists": finalists, "abstain": abstain})
            self.exile(out, tag)
            return out
        # 最高票未过半（含被弃票压过）→ 无人出局，但不重投
        if len(finalists) > 1:
            why = "平票（%s）" % list_names(finalists)
        else:
            why = ("最高票 %s 只有 %d 票、未过半（%d 人弃票）"
                   % (_fmt_weight(top), int(round(top)), len(abstain)))
        self.public("计票：%s → %s。" % (detail, why), msg="系统", share=True)
        self.emit("vote_result", {"tally": tally, "out": None, "tag": tag,
                                  "finalists": finalists, "abstain": abstain})
        if len(finalists) == 1:
            self.call_judge("vote_result", "%s；最高票未过半，本轮无人出局。" % detail)
            return None
        if not allow_revote:
            return None
        tied_voters = [s for s in self.alive_voters() if s not in finalists]
        if not tied_voters:
            self.public("平票者之外无人可投，本轮作废。", msg="系统", share=True)
            return None
        self.call_judge("vote_result", "平票，进行一轮重新投票。")
        return self._revote(finalists, tied_voters, voters_n)

    def _revote(self, finalists, tied_voters, voters_n):
        """平票后的重新投票：平票者不能投票，也不能再被投。"""
        allow_abstain = bool(self.settings["allow_abstain"])
        revote_candidates = [s for s in self.alive_seats()
                             if s not in finalists and not self.players[s].revealed]
        if len(revote_candidates) <= 1:
            self.public("平票者之外没有其他候选人，本轮无人出局。", msg="系统",
                        share=True)
            return None
        votes2 = {}
        for seat in tied_voters:
            p = self.players[seat]
            targets = [s for s in revote_candidates if s != seat]
            if not targets:
                continue
            self.emit("turn", {"seat": seat, "kind": "重投"})
            task = "出现平票，请重新投票（平票者不能投票也不再作为候选人）。"
            fmt = '{"vote": 席位号, "speech": "一句话理由"}'
            extra = ["【平票玩家】%s" % list_names(finalists),
                     "【可投票席位】%s" % list_names(targets)]
            if allow_abstain:
                task += "本轮允许弃票：不想投就把 vote 填 null。"
                fmt = '{"vote": 席位号或null（null=弃票）, "speech": "一句话理由"}'
            user = self._user_prompt(p, task, fmt=fmt, extra=extra)
            data, raw = self.ask_player(p, user, fmt_seats=targets,
                                        fallback_speech="我改投 %s。" % seat_name(targets[0]))
            target, abstained = self._vote_of(data, targets, allow_abstain)
            votes2[seat] = target
            self.emit("vote", {"voter": seat, "target": target,
                               "abstain": abstained,
                               "reason": self._speech_of(data, raw, ""),
                               "round": self.round_no, "tag": "重投"})
            self.emit("turn", {"seat": None})
            self._sleep(0.1)
        if not votes2:
            return None
        return self._settle_votes(votes2, tag="重投", allow_revote=False,
                                  voters=tied_voters, only_targets=revote_candidates)

    def exile(self, seat, tag):
        p = self.players[seat]
        if p.role == ROLE_IDIOT and p.alive and not p.revealed:
            p.revealed = True
            p.can_vote = False
            self.emit("phase", {"phase": "第%d天 · 白痴翻牌" % self.round_no,
                                "round": self.round_no})
            self.public("%s 翻开身份牌：白痴！本次放逐无效，他留在场上但失去投票权。"
                        % seat_name(seat), msg="系统", share=True)
            self.call_judge("idiot", "%s 翻牌为白痴，放逐无效。" % seat_name(seat))
            self._idiot_speak(p)
            self.push_seats()
            return
        self.kill(seat, "放逐" if tag == "放逐" else tag)
        self.emit("death", {"seat": seat, "cause": p.death_cause,
                            "reveal": self.settings["reveal_death"]})
        if self.settings["reveal_death"]:
            self.public("%s 被%s出局（身份：%s）。"
                        % (seat_name(seat), tag, p.role), msg="系统", share=True)
        else:
            self.public("%s 被%s出局。" % (seat_name(seat), tag), msg="系统",
                        share=True)
        self.maybe_shoot(seat)
        # 遗言登记：已经开过枪并说过话的猎人 / 狼王不再重复发言
        if self.settings["last_words"] and not self.players[seat].has_shot:
            self.pending_last_words.append((seat, self._death_index(seat)))

    def _idiot_speak(self, p):
        self.emit("turn", {"seat": p.seat, "kind": "翻牌发言"})
        user = self._user_prompt(
            p, "你被投票放逐，翻牌是【白痴】因此不会出局，但从此失去投票权。请说一句话。",
            fmt='{"speech": "你的话"}')
        data, raw = self.ask_player(p, user, fallback_speech="我是白痴，我翻牌了。")
        self._push(self._speech_of(data, raw, "我是白痴，我翻牌了。"), msg="翻牌",
                   seat=p.seat, detail="已失去投票权", layer="public", share=True)
        self.emit("turn", {"seat": None})
        self._sleep(0.15)

    # ------------------------------------------------------------------
    # 遗言 / 开枪
    # ------------------------------------------------------------------
    def flush_last_words(self, night=False):
        """把待发表遗言按死亡顺序说完；night=True 时只处理夜间死亡者。"""
        if not self.settings["last_words"] or not self.pending_last_words:
            return
        items = sorted(self.pending_last_words, key=lambda x: x[1])
        self.pending_last_words = []
        if not items:
            return
        self.call_judge("lastwords", "请出局者留下遗言：%s"
                        % "、".join(seat_name(s) for s, _ in items))
        for seat, _idx in items:
            self._check_stop()
            p = self.players[seat]
            self.emit("turn", {"seat": seat, "kind": "遗言"})
            user = self._user_prompt(
                p, "你已经出局了，请留下遗言（可以公布身份、指认狼人、安排后事）。",
                fmt='{"speech": "你的遗言"}')
            data, raw = self.ask_player(p, user, fallback_speech="我先走了。")
            self._push(self._speech_of(data, raw, "我先走了。"), msg="遗言",
                       seat=seat, detail="%s出局" % p.death_cause, layer="public",
                       share=True)
            self.emit("turn", {"seat": None})
            self._sleep(0.15)

    def maybe_shoot(self, seat):
        p = self.players.get(seat)
        if not p or p.role not in (ROLE_HUNTER, ROLE_WOLF_KING):
            return
        if p.death_cause == "毒杀":
            self.godview("%s 被毒杀，无法开枪。" % p.name)
            return
        if p.has_shot:                  # 每人只开一枪，避免连锁死循环
            return
        self.hunter_shoot(p)

    def hunter_shoot(self, shooter):
        shooter.has_shot = True
        self.emit("phase", {"phase": "第%d轮 · %s开枪" % (max(self.round_no, 1),
                                                         shooter.role),
                            "round": self.round_no})
        candidates = self.alive_seats()
        if not candidates:
            return
        self.call_judge("hunter", "%s（%s）出局并开枪。" % (shooter.name, shooter.role))
        self.emit("turn", {"seat": shooter.seat, "kind": "开枪"})
        user = self._user_prompt(
            shooter, "你可以开枪带走一名存活玩家；不想开枪就把 target 填 null。",
            fmt='{"speech": "你的开枪宣言", "target": 席位号或null}',
            extra=["【可开枪目标】%s" % list_names(candidates)])
        data, raw = self.ask_player(shooter, user, fmt_seats=candidates,
                                    fallback_speech="我带走 %s。" % seat_name(candidates[0]))
        target = pick_seat((data or {}).get("target"), set(candidates))
        text = self._speech_of(data, raw, "……")
        if target:
            # 开枪宣言与目标合并成一条，避免同一个人连说两遍
            self._push("%s（开枪带走 %s）" % (text, seat_name(target)), msg="开枪",
                       seat=shooter.seat, detail="", layer="public", share=True)
            self.kill(target, "枪杀")
            self.emit("death", {"seat": target, "cause": "枪杀",
                                "reveal": self.settings["reveal_death"]})
            self.public("%s 被枪杀出局。" % seat_name(target), msg="系统", share=True)
            self.push_seats()
            # 真正的连锁：被枪杀者若是猎人 / 狼王（非毒杀、未开过枪），继续开枪
            self.godview("%s 被枪杀，若他是猎人 / 狼王则继续开枪。" % seat_name(target))
            self.maybe_shoot(target)
        else:
            self._push("%s（放弃开枪）" % text, msg="开枪", seat=shooter.seat,
                       detail="", layer="public", share=True)
        self.emit("turn", {"seat": None})

    # ------------------------------------------------------------------
    # 警长竞选（第一个白天）
    # ------------------------------------------------------------------
    def sheriff_election(self):
        self.sheriff_done = True
        self.phase = "警长竞选"
        self.emit("phase", {"phase": "第%d天 · 警长竞选" % self.round_no,
                            "round": self.round_no})
        self.call_judge("sheriff", "开始警长竞选。")
        alive = self.alive_seats()
        runners = []
        for seat in alive:
            self._check_stop()
            p = self.players[seat]
            self.emit("turn", {"seat": seat, "kind": "上警"})
            user = self._user_prompt(
                p, "是否参加警长竞选？请表明态度并给出理由。",
                fmt='{"run": true/false, "speech": "你的竞选发言或弃选理由"}')
            data, raw = self.ask_player(p, user, fallback_speech="我保持低调。")
            data = data or {}
            run = data.get("run")
            if isinstance(run, str):
                run = run.strip().lower() in ("true", "1", "yes", "是", "上警")
            run = bool(run)
            text = self._speech_of(data, raw, "我上警。" if run else "我不上警。")
            if run:
                runners.append(seat)
                self.speech(p, text, kind="上警")
            else:
                self.speech(p, text, kind="表态")
            self.emit("turn", {"seat": None})
            self._sleep(0.15)
        if not runners:
            self.public("无人上警，本局不产生警长。", msg="系统", share=True)
            return
        if len(runners) == 1:
            self.sheriff_seat = runners[0]
            self.players[runners[0]].sheriff = True
            self.public("%s 是唯一竞选者，自动当选警长。" % seat_name(runners[0]),
                        msg="系统", share=True)
            self.push_seats()
            return
        for seat in runners:
            p = self.players[seat]
            self.emit("turn", {"seat": seat, "kind": "竞选演说"})
            user = self._user_prompt(
                p, "你是警长候选人，请发表竞选演说。",
                fmt='{"speech": "竞选演说（40~150字）"}')
            data, raw = self.ask_player(p, user, fallback_speech="请把警徽给我。")
            self.speech(p, self._speech_of(data, raw, "请把警徽给我。"), kind="竞选演说")
            self.emit("turn", {"seat": None})
            self._sleep(0.2)
        allow_abstain = bool(self.settings["allow_abstain"])
        votes = {}
        for seat in [s for s in self.alive_voters() if s not in runners]:
            p = self.players[seat]
            targets = [s for s in runners if s != seat]
            if not targets:
                continue
            self.emit("turn", {"seat": seat, "kind": "警长投票"})
            task = "请投票选出你支持的警长。"
            fmt = '{"vote": 席位号, "speech": "一句话理由"}'
            extra = ["【候选人】%s" % list_names(runners)]
            if allow_abstain:
                task += "允许弃票：不想投就把 vote 填 null。"
                fmt = '{"vote": 席位号或null（null=弃票）, "speech": "一句话理由"}'
            user = self._user_prompt(p, task, fmt=fmt, extra=extra)
            data, raw = self.ask_player(p, user, fmt_seats=targets,
                                        fallback_speech="我给 %s。" % seat_name(targets[0]))
            target, abstained = self._vote_of(data, targets, allow_abstain)
            votes[seat] = target
            self.emit("vote", {"voter": seat, "target": target,
                               "abstain": abstained,
                               "reason": self._speech_of(data, raw, ""),
                               "round": self.round_no, "tag": "警长"})
            self.emit("turn", {"seat": None})
            self._sleep(0.1)
        tally = {}
        abstain = []
        for voter, target in votes.items():
            if not self.players[voter].alive:
                continue
            if target is None:
                abstain.append(voter)
                continue
            tally[target] = tally.get(target, 0) + 1
        if tally:
            top = max(tally.values())
            finalists = sorted([s for s, v in tally.items() if v == top])
            self.sheriff_seat = (self.random.choice(finalists) if len(finalists) > 1
                                 else finalists[0])
        else:
            self.sheriff_seat = self.random.choice(runners)
        if abstain:
            self.public("弃票：%s" % list_names(abstain), msg="系统", share=True)
        self.players[self.sheriff_seat].sheriff = True
        self.public("%s 当选警长。" % seat_name(self.sheriff_seat), msg="系统",
                    share=True)
        self.push_seats()

    # ------------------------------------------------------------------
    # 胜负
    # ------------------------------------------------------------------
    def check_win(self):
        alive = self.alive_seats()
        wolves = self.alive_seats(camp=CAMP_WOLF)
        villagers = self.alive_seats(role=ROLE_VILLAGER)
        gods = [s for s in alive if self.players[s].is_god]
        has_villagers = ROLE_VILLAGER in self.living_roles()
        has_gods = any(p.is_god for p in self.players.values())
        mode = self.settings["wolf_win_mode"]
        if not wolves:
            self.winner = CAMP_GOOD
            self.win_reason = "所有狼人已经出局。"
        elif mode == "all":
            if not [s for s in alive if self.players[s].camp == CAMP_GOOD]:
                self.winner = CAMP_WOLF
                self.win_reason = "所有好人都已出局（屠城）。"
        else:
            if has_villagers and not villagers:
                self.winner = CAMP_WOLF
                self.win_reason = "所有平民已经出局，狼人屠边成功。"
            elif has_gods and not gods:
                self.winner = CAMP_WOLF
                self.win_reason = "所有神职已经出局，狼人屠边成功。"
        if not self.winner and len(wolves) > len(alive) - len(wolves):
            self.winner = CAMP_WOLF
            self.win_reason = "狼人数量已超过好人，投票无法再阻止狼人。"
        if self.winner:
            self.emit("phase", {"phase": "游戏结束", "round": self.round_no})
            return True
        return False

    def _final_declare(self):
        if not self.winner:
            self.winner = "平局"
        roles = "；".join("%s=%s" % (seat_name(s), self.players[s].role)
                          for s in self.seats)
        self.call_judge("win", "%s 获胜，原因：%s。全部身份：%s。"
                        % (self.winner, self.win_reason, roles))
        self.emit("final", {"winner": self.winner, "reason": self.win_reason,
                            "roles": {s: self.players[s].role for s in self.seats}})
