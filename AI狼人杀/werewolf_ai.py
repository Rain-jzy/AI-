# -*- coding: utf-8 -*-
"""
AI 狼人杀 · 全自动模拟对局（v2）
================================
N 名 AI 玩家（6~12 人）+ 1 名 AI 上帝。两种模式：

 * 经典模式   —— 4 狼人 + 4 平民 + 预言家 / 女巫 / 猎人，身份开局随机。
 * 自定义模式 —— 人数 6~12 自选、每个席位身份自选，身份池：
   狼人 / 狼王 / 白狼王（狼人阵营）、平民 / 预言家 / 女巫 / 猎人 / 守卫 / 白痴（好人阵营）。

特性
 * 运行前在「API 与 AI 席位」里为席位配置接口，允许同一个 AI 重复占多个席位。
 * 9 项游戏设置（默认全关，遗言默认开）：自爆 / 遗言 / 奶穿 / 胜负规则 /
 *   是否报身份死因 / 女巫刀口情报 / 女巫双药 / 警长 / 上下文条数。
 * 对话区三层显示：公开内容=黑色正体，狼队频道=红色，上帝视角=灰色斜体。
 * 开局后锁死所有设置入口，对局结束才解锁。
 * 实时显示每个 AI 的 token 消耗与全局累计（不估算金额）。

运行：  python werewolf_ai.py
依赖：  仅 Python 标准库（tkinter + urllib），无需 pip 安装。
"""

import json
import os
import queue
import random
import sys
import threading
import time
import tkinter as tk
import traceback
from tkinter import messagebox, ttk

APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from llm_client import (PROVIDER_PRESETS, LLMAgent, LLMError, TokenMeter,
                        fmt_num, test_connection)
from game_engine import (CAMP_GOOD, CAMP_WOLF, CUSTOM_ROLE_POOL,
                         DEFAULT_SETTINGS, GOD_ROLES, MAX_PLAYERS, MAX_GODS,
                         MIN_PLAYERS, ROLE_BRIEF, ROLE_GUARD, ROLE_HUNTER,
                         ROLE_IDIOT, ROLE_SEER, ROLE_VILLAGER,
                         ROLE_WHITE_WOLF_KING, ROLE_WITCH, ROLE_WOLF,
                         ROLE_WOLF_KING, UNIQUE_ROLES, WOLF_ROLES,
                         WerewolfGame, balance_hint, normalize_settings,
                         seat_name, validate_roles)

CONFIG_PATH = os.path.join(APP_DIR, "ai_config.json")
RECORD_DIR = os.path.join(APP_DIR, "对局记录")
JUDGE_ROW = MAX_PLAYERS + 1          # 上帝在 API 配置表里的行号

# --------------------------------------------------------------------------
# 配色：对话区为淡绿色；三种信息层的字体/颜色在这里统一定义
# --------------------------------------------------------------------------
C_BG = "#e8f7ec"          # 整体底色（淡绿）
C_CHAT_BG = "#dff3e4"     # 对话区背景（淡绿）
C_SIDE_BG = "#eefaf1"
C_DARK = "#12684a"        # 深绿
C_MID = "#2fa06d"
C_WHITE = "#ffffff"
C_TEXT = "#14392a"
C_MUTED = "#5d7d70"
C_GOLD = "#b8860b"

# 三层信息流配色
C_PUBLIC_BG = "#ffffff"       # 公开发言：白底
C_PUBLIC_FG = "#111111"       # 公开内容：黑色正体
C_WOLF_BG = "#fdecec"         # 狼队频道：浅红底
C_WOLF_FG = "#c1121f"         # 狼队内容：红色正体
C_GOD_FG = "#7a8a83"          # 上帝视角：灰色斜体
C_GOD_BG = "#f2f6f3"
C_SYSTEM_FG = "#3c5a4c"

# 阶段横幅（不再用大面积纯色）
C_BAND_NIGHT = "#e3e9f2"
C_BAND_DAY = "#e3f2e6"
C_BAND_FG = "#3a4a55"

ROLE_COLORS = {
    ROLE_WOLF: "#b3261e",
    ROLE_WOLF_KING: "#8c1c13",
    ROLE_WHITE_WOLF_KING: "#6d1a4f",
    ROLE_SEER: "#1f6fb2",
    ROLE_WITCH: "#7b3fbf",
    ROLE_HUNTER: "#a8590b",
    ROLE_GUARD: "#0e7490",
    ROLE_IDIOT: "#7a7a1f",
    ROLE_VILLAGER: "#2b7a4b",
}

# 各类发言头部颜色
KIND_HEAD = {
    "发言": "#1b5e20",
    "遗言": "#6a1b9a",
    "上警": "#8d6e00",
    "表态": "#4e6e5d",
    "竞选演说": "#b06a00",
    "行动": "#5f5f5f",
    "开枪": "#8c1c13",
    "自爆": "#c1121f",
    "翻牌": "#7a7a1f",
    "狼队频道": "#c1121f",
    "系统": "#3c5a4c",
}

UI_FONT = "Microsoft YaHei UI"
if sys.platform != "win32":
    UI_FONT = "Noto Sans CJK SC"


# --------------------------------------------------------------------------
# 配置读写
# --------------------------------------------------------------------------
def blank_seat(seat):
    return {
        "name": seat_name(seat) if seat else "上帝",
        "base_url": "",
        "api_key": "",
        "model": "",
        "temperature": 0.85,
        "max_tokens": 700,
        "timeout": 120,
        "json_mode": False,
        "retries": 3,
        "thinking": False,          # 思考模式（DeepSeek V4 / V4.1 等）
    }


def default_config():
    return {
        "seats": {str(s): blank_seat(s) for s in range(1, MAX_PLAYERS + 1)},
        "judge": dict(blank_seat(0), name="上帝"),
        "mode": "classic",                       # classic / custom
        "custom_roles": [ROLE_WOLF, ROLE_WOLF_KING, ROLE_WHITE_WOLF_KING,
                         ROLE_VILLAGER, ROLE_VILLAGER, ROLE_SEER, ROLE_WITCH,
                         ROLE_HUNTER, ROLE_GUARD, ROLE_IDIOT],
        "settings": dict(DEFAULT_SETTINGS),
    }



def load_config():
    cfg = default_config()
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            for s in range(1, MAX_PLAYERS + 1):
                got = (data.get("seats") or {}).get(str(s)) or {}
                cfg["seats"][str(s)].update(got)
            cfg["judge"].update(data.get("judge") or {})
            cfg["judge"]["name"] = "上帝"
            if data.get("mode") in ("classic", "custom"):
                cfg["mode"] = data["mode"]
            roles = data.get("custom_roles")
            if isinstance(roles, list) and MIN_PLAYERS <= len(roles) <= MAX_PLAYERS \
                    and all(r in CUSTOM_ROLE_POOL for r in roles):
                cfg["custom_roles"] = roles
            cfg["settings"] = normalize_settings(data.get("settings"))
            cfg["judge"]["name"] = "上帝"
        except Exception as e:
            print("读取配置失败：%s" % e)
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        messagebox.showerror("保存失败", str(e))
        return False


# ==========================================================================
# 主界面
# ==========================================================================
class WerewolfApp(object):

    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        self.meter = TokenMeter()
        self.events = queue.Queue()
        self.game = None
        self.game_thread = None
        self.paused = threading.Event()
        self.stopping = False
        self.start_time = None
        self.winner = None
        self.win_reason = ""
        self.roles = {}
        self.seat_rows = {}
        self.chat_records = []
        self.live_seats = {}
        self.locked = False               # 对局中锁死所有设置入口
        self._layout_mode = None

        root.title("AI 狼人杀 · 全自动 AI 对局")
        root.configure(bg=C_BG)
        self._center(1400, 880)
        self._init_style()
        self._build_menu()
        self._build_toolbar()
        self._build_body()
        self._build_status()

        self.root.after(80, self._pump)
        self.root.after(1000, self._tick)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Configure>", self._on_resize)
        self.refresh_mode_ui()
        self.log_system("欢迎来到 AI 狼人杀。步骤：①选模式 ②配置身份 ③配置 API ④"
                        "游戏设置 ⑤开始对局。")
        if not self._has_any_key():
            self.log_system("提示：还没有任何席位填写 API 地址，点击橙色按钮开始配置。")

    # ---------------------------------------------------------------- 布局
    def _center(self, w, h):
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        w = min(w, max(980, sw - 80))
        h = min(h, max(660, sh - 120))
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 3)
        self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))
        self.root.minsize(1000, 660)

    @staticmethod
    def layout_for_width(w):
        """按窗口宽度决定工具栏布局：wide / normal / compact。"""
        if w >= 1360:
            return "wide"
        if w >= 1160:
            return "normal"
        return "compact"

    def _on_resize(self, event=None):
        """窗口变窄时自动收紧工具栏，避免按钮被截断。"""
        if event is not None and event.widget is not self.root:
            return
        mode = self.layout_for_width(self.root.winfo_width())
        if mode == self._layout_mode:
            return
        self._layout_mode = mode
        try:
            if mode == "wide":
                self.lb_subtitle.pack(side="left")
                self.speed_box.pack(side="right", padx=10)
                self.lb_mode.pack(side="left", padx=(0, 4))
                self.chk_roles.pack(side="right", padx=6)
            elif mode == "normal":
                self.lb_subtitle.pack_forget()
                self.speed_box.pack(side="right", padx=8)
                self.lb_mode.pack_forget()
                self.chk_roles.pack(side="right", padx=4)
            else:
                self.lb_subtitle.pack_forget()
                self.speed_box.pack_forget()
                self.lb_mode.pack_forget()
                self.chk_roles.pack_forget()
        except AttributeError:
            pass

    def _init_style(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=C_BG, foreground=C_TEXT, font=(UI_FONT, 10))
        st.configure("TFrame", background=C_BG)
        st.configure("TLabel", background=C_BG, foreground=C_TEXT, font=(UI_FONT, 10))
        st.configure("TButton", font=(UI_FONT, 10), padding=(10, 5))
        st.configure("Accent.TButton", font=(UI_FONT, 10, "bold"), padding=(12, 5))
        st.configure("TNotebook", background=C_BG)
        st.configure("TNotebook.Tab", font=(UI_FONT, 10), padding=(12, 6))
        st.configure("Treeview", font=(UI_FONT, 10), rowheight=26,
                     background=C_WHITE, fieldbackground=C_WHITE)
        st.configure("Treeview.Heading", font=(UI_FONT, 10, "bold"))
        st.configure("TCheckbutton", background=C_BG, font=(UI_FONT, 10))
        st.configure("TLabelframe", background=C_BG)
        st.configure("TLabelframe.Label", background=C_BG,
                     foreground=C_DARK, font=(UI_FONT, 10, "bold"))

    def _build_menu(self):
        bar = tk.Menu(self.root, font=(UI_FONT, 10))
        m_file = tk.Menu(bar, tearoff=0, font=(UI_FONT, 10))
        m_file.add_command(label="开始对局", command=self.start_game)
        m_file.add_command(label="暂停 / 继续", command=self.toggle_pause)
        m_file.add_command(label="停止对局", command=self.stop_game)
        m_file.add_separator()
        m_file.add_command(label="保存对局记录…", command=self.save_record)
        m_file.add_separator()
        m_file.add_command(label="退出", command=self._on_close)
        bar.add_cascade(label="对局", menu=m_file)

        m_cfg = tk.Menu(bar, tearoff=0, font=(UI_FONT, 10))
        m_cfg.add_command(label="配置 API 与 AI 席位…", command=self.open_config)
        m_cfg.add_command(label="配置身份 / 人数…", command=self.open_roles)
        m_cfg.add_command(label="游戏设置…", command=self.open_settings)
        m_cfg.add_separator()
        m_cfg.add_command(label="切换到经典模式", command=lambda: self.set_mode("classic"))
        m_cfg.add_command(label="切换到自定义模式",
                          command=lambda: self.set_mode("custom"))
        m_cfg.add_separator()
        m_cfg.add_command(label="重载本地配置", command=self.reload_config)
        bar.add_cascade(label="配置", menu=m_cfg)
        self.menu_cfg = m_cfg

        m_help = tk.Menu(bar, tearoff=0, font=(UI_FONT, 10))
        m_help.add_command(label="玩法与说明", command=self.show_help)
        bar.add_cascade(label="帮助", menu=m_help)
        self.root.config(menu=bar)

    def _build_toolbar(self):
        bar = tk.Frame(self.root, bg=C_DARK, height=60)
        bar.pack(side="top", fill="x")
        bar.pack_propagate(False)

        tk.Label(bar, text="🐺 AI 狼人杀", bg=C_DARK, fg=C_WHITE,
                 font=(UI_FONT, 16, "bold")).pack(side="left", padx=(14, 6))
        self.lb_subtitle = tk.Label(bar, text="", bg=C_DARK, fg="#a9e5c4",
                                    font=(UI_FONT, 9))
        self.lb_subtitle.pack(side="left")

        # 模式切换
        mode_box = tk.Frame(bar, bg=C_DARK)
        mode_box.pack(side="left", padx=12)
        self.lb_mode = tk.Label(mode_box, text="模式", bg=C_DARK, fg="#cdeedd",
                                font=(UI_FONT, 9))
        self.lb_mode.pack(side="left", padx=(0, 4))
        self.mode_btn_classic = tk.Button(
            mode_box, text="经典", command=lambda: self.set_mode("classic"),
            bg="#39d98a", fg="#08351f", relief="flat", font=(UI_FONT, 9, "bold"),
            padx=10, pady=3, cursor="hand2")
        self.mode_btn_classic.pack(side="left", padx=2)
        self.mode_btn_custom = tk.Button(
            mode_box, text="自定义", command=lambda: self.set_mode("custom"),
            bg="#1c8a63", fg="#d8f3de", relief="flat", font=(UI_FONT, 9, "bold"),
            padx=10, pady=3, cursor="hand2")
        self.mode_btn_custom.pack(side="left", padx=2)
        self.btn_roles = tk.Button(mode_box, text="🎭 身份", command=self.open_roles,
                                   bg="#b5179e", fg=C_WHITE, relief="flat",
                                   activebackground="#96127f", font=(UI_FONT, 9, "bold"),
                                   padx=10, pady=3, cursor="hand2")
        self.btn_roles.pack(side="left", padx=(8, 2))
        self.btn_settings = tk.Button(mode_box, text="🛠 游戏设置",
                                      command=self.open_settings,
                                      bg="#5b7cfa", fg=C_WHITE, relief="flat",
                                      activebackground="#4a68d8",
                                      font=(UI_FONT, 9, "bold"), padx=10, pady=3,
                                      cursor="hand2")
        self.btn_settings.pack(side="left", padx=2)

        right = tk.Frame(bar, bg=C_DARK)
        right.pack(side="right", padx=10)

        self.btn_stop = tk.Button(right, text="■ 停止", command=self.stop_game,
                                  bg="#c94b3a", fg=C_WHITE, relief="flat",
                                  activebackground="#a83a2b", activeforeground=C_WHITE,
                                  font=(UI_FONT, 10, "bold"), padx=12, pady=4,
                                  state="disabled", cursor="hand2")
        self.btn_stop.pack(side="right", padx=3)

        self.btn_pause = tk.Button(right, text="⏸ 暂停", command=self.toggle_pause,
                                   bg="#f0ad4e", fg="#3a2a00", relief="flat",
                                   activebackground="#d99a3f", font=(UI_FONT, 10, "bold"),
                                   padx=12, pady=4, state="disabled", cursor="hand2")
        self.btn_pause.pack(side="right", padx=3)

        self.btn_start = tk.Button(right, text="▶ 开始对局", command=self.start_game,
                                   bg="#39d98a", fg="#08351f", relief="flat",
                                   activebackground="#2fbf78", font=(UI_FONT, 11, "bold"),
                                   padx=16, pady=4, cursor="hand2")
        self.btn_start.pack(side="right", padx=3)

        self.btn_cfg = tk.Button(right, text="⚙ API", command=self.open_config,
                                 bg="#ff9f1c", fg="#3a2200", relief="flat",
                                 activebackground="#e88e12", font=(UI_FONT, 10, "bold"),
                                 padx=12, pady=4, cursor="hand2")
        self.btn_cfg.pack(side="right", padx=3)

        self.speed_box = tk.Frame(right, bg=C_DARK)
        self.speed_box.pack(side="right", padx=8)
        tk.Label(self.speed_box, text="节奏", bg=C_DARK, fg="#cdeedd",
                 font=(UI_FONT, 9)).pack(side="left", padx=(0, 4))
        self.speed_var = tk.DoubleVar(value=1.4)
        self.speed_scale = tk.Scale(self.speed_box, from_=0.2, to=5.0, resolution=0.2,
                                    orient="horizontal", variable=self.speed_var,
                                    bg=C_DARK, fg=C_WHITE, troughcolor="#1c8a63",
                                    highlightthickness=0, bd=0, length=110,
                                    showvalue=True, font=(UI_FONT, 8),
                                    command=self._on_speed)
        self.speed_scale.pack(side="left")

        self.show_roles = tk.BooleanVar(value=True)
        self.chk_roles = tk.Checkbutton(right, text="显示身份", variable=self.show_roles,
                                       bg=C_DARK, fg="#cdeedd", selectcolor="#1c8a63",
                                       activebackground=C_DARK, activeforeground=C_WHITE,
                                       font=(UI_FONT, 9), command=self.refresh_seats)
        self.chk_roles.pack(side="right", padx=4)


    def _build_body(self):
        body = tk.Frame(self.root, bg=C_BG)
        body.pack(side="top", fill="both", expand=True, padx=10, pady=(8, 4))

        # 左：对话区（淡绿背景）
        left = tk.Frame(body, bg=C_BG)
        left.pack(side="left", fill="both", expand=True)
        head = tk.Frame(left, bg=C_MID, height=30)
        head.pack(side="top", fill="x")
        head.pack_propagate(False)
        tk.Label(head, text="  💬 对局实况 · 上帝播报 / 玩家发言", bg=C_MID, fg=C_WHITE,
                 font=(UI_FONT, 10, "bold")).pack(side="left")
        self.phase_label = tk.Label(head, text="准备中", bg=C_MID, fg="#eafff2",
                                    font=(UI_FONT, 10, "bold"))
        self.phase_label.pack(side="right", padx=12)

        wrap = tk.Frame(left, bg=C_CHAT_BG, highlightbackground=C_MID,
                        highlightthickness=1)
        wrap.pack(side="top", fill="both", expand=True)
        self.chat = tk.Text(wrap, bg=C_CHAT_BG, fg=C_TEXT, wrap="word",
                            font=(UI_FONT, 11), relief="flat", padx=20, pady=14,
                            spacing1=3, spacing3=8, insertbackground=C_DARK,
                            selectbackground="#b7e6c8", cursor="arrow")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.chat.yview)
        self.chat.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.chat.pack(side="left", fill="both", expand=True)
        self.chat.configure(state="disabled")
        self._init_tags()

        # 右：席位 / token 面板
        right = tk.Frame(body, bg=C_SIDE_BG, width=340,
                         highlightbackground=C_MID, highlightthickness=1)
        right.pack(side="right", fill="y", padx=(10, 0))
        right.pack_propagate(False)
        self._build_sidebar(right)

    def _init_tags(self):
        """三层信息流 + 气泡样式。

        公开内容 = 黑色正体；狼队频道 = 红色正体；上帝视角/私密 = 灰色斜体。
        每条玩家发言用一层浅色底 + 左右留白模拟聊天气泡。
        """
        c = self.chat
        HEAD = 26
        BODY = 26
        c.tag_configure("system", foreground=C_SYSTEM_FG, font=(UI_FONT, 9),
                        lmargin1=HEAD, lmargin2=BODY, rmargin=14, spacing3=8)
        c.tag_configure("error", foreground="#9c2b2b", background="#ffe9e9",
                        font=(UI_FONT, 9), lmargin1=HEAD, lmargin2=BODY, spacing3=8)
        # 阶段横幅：细线 + 浅底 + 居中，不再用大面积纯色
        c.tag_configure("band", foreground=C_BAND_FG, background=C_BAND_DAY,
                        font=(UI_FONT, 10, "bold"), justify="center",
                        spacing1=10, spacing3=10, lmargin1=0, lmargin2=0)
        c.tag_configure("band_night", foreground=C_BAND_FG, background=C_BAND_NIGHT,
                        font=(UI_FONT, 10, "bold"), justify="center",
                        spacing1=10, spacing3=10, lmargin1=0, lmargin2=0)
        c.tag_configure("hairline", foreground="#b9d6c4",
                        font=(UI_FONT, 6), justify="center", spacing1=2, spacing3=2)
        # 上帝播报：居中、无气泡
        c.tag_configure("god_talk", foreground="#26443a", font=(UI_FONT, 11),
                        justify="center", lmargin1=60, lmargin2=60, rmargin=60,
                        spacing3=12)
        # 气泡头部
        c.tag_configure("bubble_head", font=(UI_FONT, 10, "bold"),
                        lmargin1=HEAD, lmargin2=BODY, spacing1=6)
        c.tag_configure("bubble_head_wolf", foreground=C_WOLF_FG,
                        font=(UI_FONT, 10, "bold"), lmargin1=HEAD, lmargin2=BODY,
                        spacing1=6)
        c.tag_configure("bubble_head_self", foreground="#1f4d3a",
                        font=(UI_FONT, 10, "bold"), lmargin1=HEAD, lmargin2=BODY,
                        spacing1=6)
        # 气泡正文（公开）
        c.tag_configure("bubble", foreground=C_PUBLIC_FG, background=C_PUBLIC_BG,
                        font=(UI_FONT, 11), lmargin1=HEAD, lmargin2=BODY, rmargin=14,
                        spacing1=4, spacing3=10)
        # 气泡正文（狼队频道）
        c.tag_configure("bubble_wolf", foreground=C_WOLF_FG, background=C_WOLF_BG,
                        font=(UI_FONT, 11), lmargin1=HEAD, lmargin2=BODY, rmargin=14,
                        spacing1=4, spacing3=10)
        # 行动 / 投票：细一点的公开文字
        c.tag_configure("action", foreground="#4d5f57", font=(UI_FONT, 10),
                        lmargin1=HEAD + 16, lmargin2=HEAD + 16, rmargin=14, spacing3=8)
        c.tag_configure("vote", foreground="#33553f", font=(UI_FONT, 10),
                        lmargin1=HEAD + 16, lmargin2=HEAD + 16, rmargin=14, spacing3=5)
        # 上帝视角 / 私密：灰色斜体
        c.tag_configure("godview", foreground=C_GOD_FG, background=C_GOD_BG,
                        font=(UI_FONT, 10, "italic"), lmargin1=HEAD, lmargin2=BODY,
                        rmargin=14, spacing3=8)
        c.tag_configure("meta", foreground=C_MUTED, font=(UI_FONT, 9))
        c.tag_configure("note_detail", foreground="#8a9a92", font=(UI_FONT, 9),
                        lmargin1=HEAD + 16, lmargin2=HEAD + 16, spacing3=8)


    def _build_sidebar(self, parent):
        head = tk.Frame(parent, bg=C_DARK, height=30)
        head.pack(side="top", fill="x")
        head.pack_propagate(False)
        tk.Label(head, text="  📊 token 消耗 / 席位状态", bg=C_DARK, fg=C_WHITE,
                 font=(UI_FONT, 10, "bold")).pack(side="left")

        box = tk.Frame(parent, bg=C_SIDE_BG)
        box.pack(side="top", fill="x", padx=8, pady=8)
        self.lb_total = tk.Label(box, text="总消耗 0 tokens", bg=C_SIDE_BG, fg=C_DARK,
                                 font=(UI_FONT, 13, "bold"), anchor="w")
        self.lb_total.pack(fill="x")
        self.lb_detail = tk.Label(box, text="输入 0 ｜ 输出 0", bg=C_SIDE_BG,
                                  fg=C_MUTED, font=(UI_FONT, 9), anchor="w",
                                  justify="left")
        self.lb_detail.pack(fill="x")
        self.lb_calls = tk.Label(box, text="调用 0 次 ｜ 失败 0 次 ｜ 用时 00:00",
                                 bg=C_SIDE_BG, fg=C_MUTED, font=(UI_FONT, 9), anchor="w")
        self.lb_calls.pack(fill="x")
        self.bar = tk.Canvas(box, height=8, bg="#cfe9d8", highlightthickness=0)
        self.bar.pack(fill="x", pady=(6, 0))
        self._bar_rect = self.bar.create_rectangle(0, 0, 0, 8, fill=C_MID, width=0)

        tk.Label(parent, text="点击某一行可单独修改该 AI 席位的接口",
                 bg=C_SIDE_BG, fg=C_MUTED, font=(UI_FONT, 8)).pack(fill="x", padx=10)

        canvas = tk.Canvas(parent, bg=C_SIDE_BG, highlightthickness=0)
        sbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        holder = tk.Frame(canvas, bg=C_SIDE_BG)
        canvas.create_window((0, 0), window=holder, anchor="nw", width=316)
        canvas.configure(yscrollcommand=sbar.set)
        sbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        holder.bind("<Configure>",
                    lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

        def _wheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        def _bind_wheel(_):
            canvas.bind_all("<MouseWheel>", _wheel)

        def _unbind_wheel(_):
            canvas.unbind_all("<MouseWheel>")

        canvas.bind("<Enter>", _bind_wheel)
        canvas.bind("<Leave>", _unbind_wheel)
        holder.bind("<Enter>", _bind_wheel)

        self.seat_rows = {}
        for i, seat in enumerate(range(1, MAX_PLAYERS + 1)):
            self.seat_rows[seat] = self._make_row(holder, seat, i)
        self._make_judge_row(holder)

    def _make_row(self, parent, seat, index):
        bg = "#ffffff" if index % 2 == 0 else "#f6fdf8"
        row = tk.Frame(parent, bg=bg, highlightbackground="#dcefe2",
                       highlightthickness=0, cursor="hand2")
        row.pack(fill="x", padx=6, pady=1)

        num = tk.Label(row, text="%d" % seat, bg=bg, fg=C_WHITE, width=3,
                       font=(UI_FONT, 10, "bold"))
        num.pack(side="left", padx=(2, 6), pady=4)
        num.configure(bg=C_MID)

        info = tk.Frame(row, bg=bg)
        info.pack(side="left", fill="x", expand=True)
        line1 = tk.Label(info, text="AI 未配置", bg=bg, fg=C_TEXT, anchor="w",
                         font=(UI_FONT, 9, "bold"))
        line1.pack(fill="x")
        line2 = tk.Label(info, text="等待开始", bg=bg, fg=C_MUTED, anchor="w",
                         font=(UI_FONT, 8))
        line2.pack(fill="x")

        tok = tk.Label(row, text="0", bg=bg, fg=C_GOLD, anchor="e",
                       font=(UI_FONT, 9, "bold"), width=8)
        tok.pack(side="right", padx=6)

        widgets = [row, num, info, line1, line2, tok]
        for w in widgets:
            w.bind("<Button-1>", lambda e, s=seat: self.open_config(seat))
        return {"frame": row, "num": num, "info": info, "line1": line1,
                "line2": line2, "tok": tok, "bg": bg, "widgets": widgets}

    def _make_judge_row(self, parent):
        row = tk.Frame(parent, bg="#fff8e6", highlightbackground="#f0dca8",
                       highlightthickness=1)
        row.pack(fill="x", padx=6, pady=(6, 10))
        tk.Label(row, text="上", bg=C_GOLD, fg=C_WHITE, width=3,
                 font=(UI_FONT, 10, "bold")).pack(side="left", padx=(2, 6), pady=4)
        info = tk.Frame(row, bg="#fff8e6")
        info.pack(side="left", fill="x", expand=True)
        self.lb_judge_name = tk.Label(info, text="上帝 AI 未配置", bg="#fff8e6",
                                      fg="#7a5a00", anchor="w", font=(UI_FONT, 9, "bold"))
        self.lb_judge_name.pack(fill="x")
        tk.Label(info, text="流程播报 / 判决 / 胜负宣告", bg="#fff8e6", fg="#9a7b2a",
                 anchor="w", font=(UI_FONT, 8)).pack(fill="x")
        self.lb_judge_tok = tk.Label(row, text="0", bg="#fff8e6", fg=C_GOLD,
                                     anchor="e", font=(UI_FONT, 9, "bold"), width=8)
        self.lb_judge_tok.pack(side="right", padx=6)
        row.bind("<Button-1>", lambda e: self.open_config(11))

    def _build_status(self):
        bar = tk.Frame(self.root, bg="#cfe9d8", height=26)
        bar.pack(side="bottom", fill="x")
        bar.pack_propagate(False)
        self.lb_status = tk.Label(bar, text="就绪", bg="#cfe9d8", fg="#1d4b37",
                                  font=(UI_FONT, 9), anchor="w")
        self.lb_status.pack(side="left", padx=12)
        self.lb_status_r = tk.Label(bar, text="", bg="#cfe9d8", fg="#1d4b37",
                                    font=(UI_FONT, 9), anchor="e")
        self.lb_status_r.pack(side="right", padx=12)

    # ------------------------------------------------------------ 对话渲染
    def _chat_enabled(self):
        self.chat.configure(state="normal")

    def _chat_done(self):
        self.chat.configure(state="disabled")
        self.chat.see("end")

    def log_system(self, text):
        self._chat_enabled()
        self.chat.insert("end", "· %s\n" % text, ("system",))
        self._chat_done()

    def log_error(self, text):
        self._chat_enabled()
        self.chat.insert("end", "⚠ %s\n" % text, ("error",))
        self._chat_done()

    def _band(self, text, night=False):
        """阶段横幅：细线 + 浅色底 + 居中，不再用大面积纯色。"""
        self._chat_enabled()
        self.chat.insert("end", "─" * 64 + "\n", ("hairline",))
        self.chat.insert("end", text + "\n",
                         ("band_night" if night else "band",))
        self.chat.insert("end", "─" * 64 + "\n", ("hairline",))
        self._chat_done()

    def render_chat(self, ev):
        seat = ev.get("seat")
        text = ev.get("text") or ""
        kind = ev.get("msg") or "发言"
        layer = ev.get("layer") or "public"
        self.chat_records.append(ev)

        # 上帝播报：居中、无气泡
        if seat is None and layer == "public" and kind in ("上帝", "系统"):
            self._chat_enabled()
            if kind == "上帝":
                self.chat.insert("end", text + "\n", ("god_talk",))
            else:
                self.chat.insert("end", "· " + text + "\n", ("system",))
            self._chat_done()
            return
        if seat is None:
            self._chat_enabled()
            self.chat.insert("end", "· %s\n" % text, ("system",))
            self._chat_done()
            return

        seat = int(seat)
        role = self.roles.get(seat, "")
        model = (self.cfg["seats"].get(str(seat), {}) or {}).get("model") or "未配置模型"
        model = model.split("/")[-1]
        is_wolf_bubble = (layer == "wolf" or kind == "狼队频道")

        # 头部：玩家名（模型名）+ 编号 + 发言类型
        head_tag = "bubble_head_wolf" if is_wolf_bubble else ("bubble_head_%d" % seat)
        if not is_wolf_bubble:
            self.chat.tag_configure(
                head_tag, foreground=ROLE_COLORS.get(role, "#1f4d3a"),
                font=(UI_FONT, 10, "bold"), lmargin1=26, lmargin2=26, spacing1=6)
        kind_tag = "kind_%s" % kind
        self.chat.tag_configure(kind_tag, foreground=KIND_HEAD.get(kind, C_MUTED),
                                font=(UI_FONT, 9, "bold"))
        role_tag = "role_%s" % (role or "x")
        self.chat.tag_configure(role_tag, foreground=ROLE_COLORS.get(role, C_MUTED),
                                font=(UI_FONT, 9, "bold"))

        self._chat_enabled()
        self.chat.insert("end", "%s  ｜  %s" % (seat_name(seat), model), (head_tag,))
        self.chat.insert("end", "   [%s]" % kind, (kind_tag,))
        if role and self.show_roles.get():
            self.chat.insert("end", "   %s" % role, (role_tag,))
        if is_wolf_bubble:
            self.chat.insert("end", "   （仅狼阵营与观众可见）", ("meta",))
        self.chat.insert("end", "\n" + text + "\n",
                         ("bubble_wolf" if is_wolf_bubble else "bubble",))
        if ev.get("detail"):
            self.chat.insert("end", "↳ %s\n" % ev["detail"], ("note_detail",))
        self._chat_done()

    def render_note(self, text, msg="上帝视角"):
        """观众层：灰色斜体。"""
        self._chat_enabled()
        self.chat.insert("end", "（%s）%s\n" % (msg, text), ("godview",))
        self._chat_done()

    def render_vote(self, info):
        target = info.get("target")
        if target is None or info.get("abstain"):
            line = "%s 弃票" % seat_name(info["voter"])
        else:
            line = "%s 投票 → %s" % (seat_name(info["voter"]), seat_name(target))
        reason = info.get("reason")
        self._chat_enabled()
        self.chat.insert("end", "🗳 %s" % line, ("vote",))
        if reason:
            self.chat.insert("end", "：%s" % reason, ("meta",))
        self.chat.insert("end", "\n")
        self._chat_done()

    # ------------------------------------------------------------ 席位面板
    def _row_set(self, row, line1=None, line2=None, num_bg=None, tok=None):
        if line1 is not None:
            row["line1"].configure(text=line1)
        if line2 is not None:
            row["line2"].configure(text=line2)
        if num_bg is not None:
            row["num"].configure(bg=num_bg)
        if tok is not None:
            row["tok"].configure(text=tok)

    def refresh_seats(self):
        active = set(self.effective_seats())
        for seat in self.seat_rows:
            if seat not in active:
                self.seat_rows[seat]["frame"].pack_forget()
        for seat in sorted(active):
            self.seat_rows[seat]["frame"].pack(fill="x", padx=6, pady=1)
            self._refresh_seat(seat)
        self._refresh_judge()

    def _refresh_seat(self, seat):
        row = self.seat_rows[seat]
        live = self.live_seats.get(seat) or {}
        cfg = self.cfg["seats"][str(seat)]
        model = cfg.get("model") or "（未配置模型）"
        roles = self.effective_roles()
        preset = roles[seat - 1] if roles and seat <= len(roles) else ""
        name = self._seat_role_label(seat)
        alive = live.get("alive", True)
        role = self.roles.get(seat) or live.get("role") or preset or ""
        tokens = self.meter.agent_total(seat_name(seat))

        if not cfg.get("base_url"):
            line1 = "AI 未配置"
        else:
            line1 = "%s · %s" % (name, model.split("/")[-1])
        bits = []
        if self.show_roles.get() and role:
            bits.append(role)
        if live.get("sheriff"):
            bits.append("🎖警长")
        if live.get("revealed"):
            bits.append("已翻牌")
        if not self.game:
            bits.append("待开局")
        elif not live:
            bits.append("准备中")
        elif not alive:
            bits.append("已出局·%s" % (live.get("death") or ""))
        else:
            bits.append("存活")
        if live.get("can_vote") is False and alive:
            bits.append("无投票权")
        line2 = " ｜ ".join(b for b in bits if b)

        num_bg = C_MID
        if role and self.show_roles.get():
            num_bg = ROLE_COLORS.get(role, C_MID)
        if self.game and not alive:
            num_bg = "#9aa8a1"
        row["num"].configure(bg=num_bg)
        self._row_set(row, line1=line1, line2=line2,
                      tok=fmt_num(tokens) if tokens else "0")

    def _refresh_judge(self):
        j = self.cfg["judge"]
        self.lb_judge_name.configure(
            text="上帝 AI · %s" % (j.get("model") or "（未配置模型）"))
        self.lb_judge_tok.configure(text=fmt_num(self.meter.agent_total("上帝")))

    def refresh_tokens(self):
        m = self.meter
        self.lb_total.configure(text="总消耗 %s tokens" % fmt_num(m.total))
        est = "（含估算）" if any(c.get("estimated") for c in m.per_call[-20:]) else ""
        self.lb_detail.configure(
            text="输入 %s ｜ 输出 %s %s"
                 % (fmt_num(m.total_prompt), fmt_num(m.total_completion), est))
        el = int(time.time() - self.start_time) if self.start_time else 0
        self.lb_calls.configure(
            text="调用 %d 次 ｜ 失败 %d 次 ｜ 用时 %02d:%02d"
                 % (m.total_calls, m.total_errors, el // 60, el % 60))
        self.lb_status_r.configure(
            text="累计 %s tokens（输入 %s ｜ 输出 %s）"
                 % (fmt_num(m.total), fmt_num(m.total_prompt),
                    fmt_num(m.total_completion)))
        # 进度条：相对显示 token 增长（对数感）
        w = max(1, self.bar.winfo_width())
        frac = min(1.0, (m.total / 200000.0))
        self.bar.coords(self._bar_rect, 0, 0, int(w * frac), 8)

    # ------------------------------------------------------------ 事件循环
    def _pump(self):
        try:
            n = 0
            while n < 400:
                ev = self.events.get_nowait()
                self._handle(ev)
                n += 1
        except queue.Empty:
            pass
        except Exception:
            traceback.print_exc()
        self.root.after(70, self._pump)

    def _handle(self, ev):
        if isinstance(ev, str):            # 容错：意外混入的纯文本事件
            self.log_error(ev)
            return
        kind = ev.get("kind")
        if kind == "chat":
            self.render_chat(ev)
        elif kind == "phase":
            self._on_phase(ev)
        elif kind == "turn":
            self._on_turn(ev)
        elif kind == "seats":
            self._on_seats(ev)
        elif kind == "roles":
            self.roles = {int(k): v for k, v in (ev.get("roles") or {}).items()}
            self.refresh_seats()
        elif kind == "tokens":
            self._on_tokens(ev)
        elif kind == "vote":
            self.render_vote(ev)
        elif kind == "death":
            self._on_death(ev)
        elif kind == "vote_result":
            self._on_vote_result(ev)
        elif kind == "error":
            self.log_error(ev.get("text", "未知错误"))
            if ev.get("trace"):
                print(ev["trace"])
        elif kind == "final":
            self._on_final(ev)
        elif kind == "game_over":
            self._on_game_over(ev)

    def _on_phase(self, ev):
        phase = ev.get("phase", "")
        self.phase_label.configure(text=phase)
        self.lb_status.configure(text=phase)
        if phase:
            self._band(phase, night=bool(ev.get("is_night")))

    def _on_turn(self, ev):
        seat = ev.get("seat")
        kind = ev.get("kind", "")
        for s, row in self.seat_rows.items():
            if s == seat:
                row["frame"].configure(highlightbackground="#f0ad4e",
                                       highlightthickness=2)
                row["frame"].configure(bg="#fff6e5")
            else:
                row["frame"].configure(highlightbackground="#dcefe2",
                                       highlightthickness=0)
                row["frame"].configure(bg=row["bg"])
        if seat:
            self.lb_status.configure(text="%s 正在%s…" % (seat_name(seat), kind))
        else:
            self.lb_status.configure(text=self.phase_label.cget("text"))

    def _on_seats(self, ev):
        for p in ev.get("players", []):
            self.live_seats[int(p["seat"])] = p
        self.refresh_seats()

    def _on_tokens(self, ev):
        self.refresh_tokens()
        pa = ev.get("per_agent") or {}
        for seat in self.effective_seats():
            b = pa.get(seat_name(seat))
            total = (b["prompt"] + b["completion"]) if b else 0
            self.seat_rows[seat]["tok"].configure(
                text=fmt_num(total) if total else "0")
        jb = pa.get("上帝")
        self.lb_judge_tok.configure(
            text=fmt_num((jb["prompt"] + jb["completion"]) if jb else 0))

    def _on_death(self, ev):
        seat = int(ev["seat"])
        role = self.roles.get(seat)
        live = self.live_seats.get(seat) or {}
        live["alive"] = False
        live["death"] = ev.get("cause", "出局")
        self.live_seats[seat] = live
        reveal = bool(ev.get("reveal"))
        self._band("%s 出局（%s）%s"
                   % (seat_name(seat), ev.get("cause", ""),
                      " · 身份：%s" % role if (role and reveal) else ""),
                   night=False)
        if role and not reveal:
            self.render_note("%s 的真实身份是 %s" % (seat_name(seat), role))
        self._refresh_seat(seat)
        self._refresh_seat(seat)

    def _on_vote_result(self, ev):
        tally = ev.get("tally") or {}
        abstain = ev.get("abstain") or []
        text = "、".join("%s %s票" % (seat_name(int(s)), _w(v))
                         for s, v in sorted(tally.items(), key=lambda kv: -kv[1]))
        tag = ev.get("tag", "放逐")
        out = ev.get("out")
        head = "【%s 计票】" % tag
        body = text if text else "无人得票"
        if abstain:
            body += "　｜　弃票：%s" % "、".join(seat_name(int(s)) for s in abstain)
        if out:
            body += "　→　%s 出局" % seat_name(int(out))
        elif ev.get("finalists"):
            body += "　→　平票：%s" % "、".join(seat_name(int(s))
                                              for s in ev["finalists"])
        else:
            body += "　→　无人出局"
        self._band("%s %s" % (head, body), night=False)

    def _on_final(self, ev):
        roles = ev.get("roles") or {}
        lines = "　".join("%s=%s" % (seat_name(int(s)), roles[s])
                         for s in sorted(roles, key=lambda x: int(x)))
        self._band("★ 最终身份公示 ★　%s" % lines, night=False)
        self.winner = ev.get("winner")
        self.win_reason = ev.get("reason", "")

    def _on_game_over(self, ev):
        winner = ev.get("winner")
        reason = ev.get("reason", "")
        self.lb_status.configure(text="对局结束：%s" % (winner or "已停止"))
        self.phase_label.configure(text="对局结束")
        self.game = None
        self.set_locked(False)
        self.btn_start.configure(state="normal", text="▶ 再来一局")
        self.btn_pause.configure(state="disabled", text="⏸ 暂停")
        self.btn_stop.configure(state="disabled")
        self.refresh_seats()
        if winner:
            self._band("🏆 %s 获胜！%s" % (winner, reason), night=False)
            messagebox.showinfo(
                "对局结束",
                "%s 获胜！\n%s\n\n本局共消耗 %s tokens，调用 %d 次。"
                % (winner, reason, fmt_num(ev.get("tokens", 0)), ev.get("calls", 0)))

    # ------------------------------------------------------------ 锁定设置
    def set_locked(self, locked):
        """对局开始后锁死所有设置入口，结束后解锁。"""
        self.locked = locked
        state = "disabled" if locked else "normal"
        for btn in (self.mode_btn_classic, self.mode_btn_custom, self.btn_roles,
                    self.btn_settings, self.btn_cfg):
            try:
                btn.configure(state=state)
            except tk.TclError:
                pass
        try:
            self.menu_cfg.entryconfigure("配置 API 与 AI 席位…", state=state)
            self.menu_cfg.entryconfigure("配置身份 / 人数…", state=state)
            self.menu_cfg.entryconfigure("游戏设置…", state=state)
            self.menu_cfg.entryconfigure("切换到经典模式", state=state)
            self.menu_cfg.entryconfigure("切换到自定义模式", state=state)
        except tk.TclError:
            pass
        self.lb_subtitle.configure(
            text="🔒 对局进行中，设置已锁定" if locked else self.mode_subtitle())

    def mode_subtitle(self):
        return ("自定义模式：%s" % "、".join(self.effective_roles() or [])
                if self.cfg.get("mode") == "custom"
                else "经典模式（%d 人，身份随机）" % len(self.effective_seats()))

    # ------------------------------------------------------------ 运行控制
    def _collect_configs(self, need_check=True):
        seats = {}
        judge = dict(self.cfg["judge"])
        judge["name"] = "上帝"
        missing = []
        for s in self.effective_seats():
            c = dict(self.cfg["seats"][str(s)])
            c["name"] = seat_name(s)
            if not c.get("base_url") or not c.get("model"):
                missing.append(seat_name(s))
            seats[s] = c
        if not judge.get("base_url") or not judge.get("model"):
            missing.append("上帝")
        if missing and need_check:
            messagebox.showwarning(
                "还有席位没配置好",
                "以下席位缺少 API 地址或模型名：\n%s\n\n请点击【⚙ API】填写。"
                % "、".join(missing))
            return None, None
        return seats, judge

    def _seat_role_label(self, seat):
        return seat_name(seat)

    def start_game(self):
        if self.game is not None:
            messagebox.showinfo("提示", "对局正在进行中。")
            return
        roles = self.effective_roles()
        if roles:
            ok, msg, stat = validate_roles(roles)
            if not ok:
                messagebox.showwarning("身份配置有误", msg)
                return
        seats, judge = self._collect_configs()
        if seats is None:
            return
        self.meter = TokenMeter()
        self.roles = {s: roles[s - 1] for s in range(1, len(roles) + 1)} if roles else {}
        self.live_seats = {}
        self.chat_records = []
        self.stopping = False
        self.paused.clear()
        self.start_time = time.time()
        self.chat.configure(state="normal")
        self.chat.delete("1.0", "end")
        self.chat.configure(state="disabled")
        self.refresh_tokens()
        self.refresh_seats()
        self.set_locked(True)

        models = {}
        for s in self.effective_seats():
            key = seats[s].get("model") or "未配置"
            models.setdefault(key, []).append(seat_name(s))
        self.log_system("对局开始（%s，%d 人）。席位分配：%s"
                        % ("自定义模式" if roles else "经典模式",
                           len(self.effective_seats()),
                           "；".join("%s → %s" % (m, "、".join(v))
                                     for m, v in models.items())))
        self.log_system("上帝 AI：%s" % (judge.get("model") or "未配置"))
        st = normalize_settings(self.cfg.get("settings"))
        on = [k for k, v in st.items() if v is True]
        if roles:
            _, _, stat = validate_roles(roles)
            self._band("🎬 自定义模式开始 · %s　（%s）"
                       % ("、".join(roles), balance_hint(stat)), night=False)
        else:
            self._band("🎬 经典模式开始 · %d 人，身份开局随机" % len(self.effective_seats()),
                       night=False)
        self.log_system("已开启的设置：%s"
                        % ("、".join(on) if on else "无（均为默认）"))

        self.game = WerewolfGame(seats, judge, self._emit, self.meter,
                                 speed=self.speed_var.get(), roles=roles,
                                 settings=self.cfg.get("settings"))
        self.game_thread = threading.Thread(target=self._run_engine,
                                            args=(self.game,), daemon=True)
        self.game_thread.start()
        self.btn_start.configure(state="disabled", text="▶ 进行中…")
        self.btn_pause.configure(state="normal", text="⏸ 暂停")
        self.btn_stop.configure(state="normal")
        self.lb_status.configure(text="对局进行中")

    def _emit(self, kind, payload):
        """引擎回调：把 (事件类型, 数据) 包装成一个 dict 投进事件队列。

        注意：引擎 emit 的签名是 emit(kind, payload)，而 queue.put 只接受一个参数，
        所以这里必须自己包装，不能直接把 self.events.put 交给引擎。
        """
        ev = dict(payload) if isinstance(payload, dict) else {"value": payload}
        ev["kind"] = kind
        self.events.put(ev)

    def _run_engine(self, game):
        try:
            game.run()
        except Exception as e:
            self.events.put({"kind": "error",
                             "text": "引擎线程异常：%s: %s" % (type(e).__name__, e)})

    def toggle_pause(self):
        if not self.game:
            return
        if self.paused.is_set():
            self.paused.clear()
            self.game.paused = False
            self.btn_pause.configure(text="⏸ 暂停")
            self.log_system("▶ 继续对局")
        else:
            self.paused.set()
            self.game.paused = True
            self.btn_pause.configure(text="▶ 继续")
            self.log_system("⏸ 已暂停（当前正在进行的 AI 调用结束后生效）")

    def stop_game(self):
        if not self.game:
            return
        self.stopping = True
        self.game.stop_flag.set()
        self.game.paused = False
        self.paused.clear()
        self.btn_stop.configure(state="disabled")
        self.btn_pause.configure(state="disabled")
        self.log_system("正在停止对局…")

    def _on_speed(self, _=None):
        if self.game:
            self.game.speed = max(0.0, float(self.speed_var.get()))

    def _on_close(self):
        if self.game and not self.stopping:
            if not messagebox.askyesno("退出", "对局还在进行，确定退出吗？"):
                return
        if self.game:
            self.game.stop_flag.set()
        self.root.destroy()

    def _tick(self):
        if self.game:
            self.refresh_tokens()
            for seat in self.effective_seats():
                t = self.meter.agent_total(seat_name(seat))
                self.seat_rows[seat]["tok"].configure(text=fmt_num(t) if t else "0")
            self._refresh_judge()
        self.root.after(1000, self._tick)

    # ------------------------------------------------------------ 模式 / 身份
    def effective_seats(self):
        """当前模式下参战的玩家席位。"""
        if self.cfg.get("mode") == "custom":
            roles = self.cfg.get("custom_roles") or []
            n = len(roles)
            if not (MIN_PLAYERS <= n <= MAX_PLAYERS):
                n = 10
            return list(range(1, n + 1))
        return list(range(1, 12))

    def effective_roles(self):
        """当前模式下的固定身份表；经典模式返回 None（随机）。"""
        if self.cfg.get("mode") == "custom":
            return list(self.cfg.get("custom_roles") or [])
        return None

    def set_mode(self, mode):
        if self.game or self.locked:
            messagebox.showinfo("提示", "对局进行中，设置已锁定。")
            return
        if mode == "custom":
            roles = self.cfg.get("custom_roles") or []
            ok, msg, stat = validate_roles(roles)
            if not ok:
                messagebox.showwarning("身份配置有误", msg)
                self.open_roles()
                return
        self.cfg["mode"] = mode
        save_config(self.cfg)
        self.refresh_mode_ui()
        self.refresh_seats()
        self.log_system("已切换到「%s」。" % ("自定义模式" if mode == "custom"
                                            else "经典模式"))

    def refresh_mode_ui(self):
        custom = self.cfg.get("mode") == "custom"
        if custom:
            self.mode_btn_classic.configure(bg="#1c8a63", fg="#d8f3de")
            self.mode_btn_custom.configure(bg="#39d98a", fg="#08351f")
        else:
            self.mode_btn_classic.configure(bg="#39d98a", fg="#08351f")
            self.mode_btn_custom.configure(bg="#1c8a63", fg="#d8f3de")
        if not self.locked:
            self.lb_subtitle.configure(text=self.mode_subtitle())

    def open_roles(self):
        if self.game or self.locked:
            messagebox.showinfo("提示", "对局进行中，设置已锁定。")
            return
        dlg = RolesDialog(self)
        self.root.wait_window(dlg.top)
        self.refresh_mode_ui()
        self.refresh_seats()

    def open_settings(self):
        if self.game or self.locked:
            messagebox.showinfo("提示", "对局进行中，设置已锁定。")
            return
        dlg = SettingsDialog(self)
        self.root.wait_window(dlg.top)
        self.refresh_mode_ui()
        self.log_system("游戏设置已更新：%s"
                        % "、".join("%s=%s" % (k, v) for k, v in
                                    sorted(self.cfg.get("settings", {}).items())))

    def reload_config(self):
        self.cfg = load_config()
        self.refresh_mode_ui()
        self.refresh_seats()
        self.log_system("已重新读取本地配置。")

    def open_config(self, seat=None):
        """打开 API / 席位配置对话框（seat=11 表示上帝席位）。"""
        if self.game or self.locked:
            messagebox.showinfo("提示", "对局进行中，设置已锁定。")
            return
        dlg = ConfigDialog(self)
        if seat in range(1, MAX_PLAYERS + 1):
            dlg._select(int(seat))
        self.root.wait_window(dlg.top)
        self.refresh_seats()
        self.refresh_tokens()

    def _has_any_key(self):
        for s in range(1, MAX_PLAYERS + 1):
            if self.cfg["seats"][str(s)].get("base_url"):
                return True
        return bool(self.cfg["judge"].get("base_url"))

    # ------------------------------------------------------------ 记录导出
    def save_record(self, path=None, silent=False):
        if not self.chat_records:
            if not silent:
                messagebox.showinfo("提示", "还没有可保存的对局内容。")
            return None
        try:
            if path is None:
                if not os.path.isdir(RECORD_DIR):
                    os.makedirs(RECORD_DIR)
                path = os.path.join(RECORD_DIR, "对局_%s.txt"
                                    % time.strftime("%Y%m%d_%H%M%S"))
            with open(path, "w", encoding="utf-8") as f:
                f.write("AI 狼人杀对局记录  %s\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
                f.write("模式：%s\n" % ("自定义模式（%d 人）" % len(self.effective_seats())
                                        if self.effective_roles()
                                        else "经典模式（11 人，身份随机）"))
                st = normalize_settings(self.cfg.get("settings"))
                f.write("设置：%s\n" % "、".join("%s=%s" % (k, v)
                                                for k, v in sorted(st.items())))
                f.write("席位配置：\n")
                for s in self.effective_seats():
                    c = self.cfg["seats"][str(s)]
                    f.write("  %s：%s（%s）\n" % (seat_name(s),
                                                 c.get("model") or "未配置",
                                                 self.roles.get(s, "-")))
                f.write("  上帝：%s\n" % (self.cfg["judge"].get("model") or "未配置"))
                f.write("\n%s\n\n" % self.meter.summary_text())
                for ev in self.chat_records:
                    who = "上帝" if ev.get("seat") is None else seat_name(int(ev["seat"]))
                    f.write("[%s][%s] %s\n" % (who, ev.get("msg") or "发言",
                                               ev.get("text", "")))
                    if ev.get("detail"):
                        f.write("    ↳ %s\n" % ev["detail"])
            self.log_system("对局记录已保存：%s" % path)
            if not silent:
                messagebox.showinfo("已保存", path)
            return path
        except Exception as e:
            if not silent:
                messagebox.showerror("保存失败", str(e))
            return None

    def show_help(self):
        messagebox.showinfo(
            "玩法与说明",
            "【两种模式】\n"
            "· 经典模式：4 狼人 + 4 平民 + 预言家/女巫/猎人，身份开局随机。\n"
            "· 自定义模式：人数 6~12 自选、每个席位身份自选，身份池为\n"
            "　狼人 / 狼王 / 白狼王（狼人阵营）、平民 / 预言家 / 女巫 / 猎人 / 守卫 / 白痴（好人阵营）\n"
            "　除狼人和平民外每个身份最多 1 个，神职最多 5 个。\n\n"
            "【流程】夜晚（狼人刀人 → 守卫守护 → 预言家验人 → 女巫用药）→ 天亮公布死讯 →\n"
            "　　　　遗言 → 第一个白天进行警长竞选（若开启）→ 依次发言 → 投票放逐 → 循环。\n"
            "【自爆】狼人在任意玩家发言结束后可自爆：自爆者出局，当天剩余流程取消，直接进黑夜。\n"
            "　　　　狼王自爆不触发开枪；白狼王自爆可带走任意一名玩家（带走猎人会触发开枪）。\n"
            "【守卫】不能连续两晚守同一人，也不能连续两晚空守（默认不开奶穿）。\n"
            "【白痴】被投票放逐时翻牌不死，但失去投票权，仍可发言。\n"
            "【遗言】第一晚出局者（除毒杀）有遗言；第二晚起夜间出局无遗言；\n"
            "　　　　所有白天出局（除自爆）都有遗言，按死亡顺序在整轮最后说。\n"
            "【胜负】狼人全灭 → 好人胜；平民或神职全灭（屠边）或好人全灭（屠城）→ 狼人胜。\n\n"
            "【对话区颜色】黑色正体=所有人都能看到；红色=狼队频道（仅狼阵营与观众）；\n"
            "　　　　　　　灰色斜体=只有上帝（你）能看到的信息。\n"
            "【设置】工具栏【🛠 游戏设置】里可开关自爆/遗言/奶穿/胜负规则/报身份死因/\n"
            "　　　　女巫刀口情报/女巫双药/警长/上下文条数。对局开始后设置自动锁定。\n"
            "【token】右侧面板实时显示总额与每个席位的消耗；上帝 AI 的消耗单独统计。")


def _w(v):
    return str(int(v)) if abs(v - int(v)) < 1e-9 else ("%.1f" % v)


# ==========================================================================
# 身份配置对话框（自定义模式）
# ==========================================================================
class RolesDialog(object):
    """开局前配置人数（6~12）与每个席位的身份。

    规则：除「狼人」「平民」外，其余身份每个最多出现 1 次；神职最多 5 个。
    """

    def __init__(self, app):
        self.app = app
        self.cfg = app.cfg
        saved = list(self.cfg.get("custom_roles") or [])
        if not (MIN_PLAYERS <= len(saved) <= MAX_PLAYERS):
            saved = [ROLE_WOLF, ROLE_WOLF_KING, ROLE_WHITE_WOLF_KING,
                     ROLE_VILLAGER, ROLE_VILLAGER, ROLE_SEER, ROLE_WITCH,
                     ROLE_HUNTER, ROLE_GUARD, ROLE_IDIOT]
        self.top = tk.Toplevel(app.root)
        self.top.title("配置人数与身份（自定义模式）")
        self.top.configure(bg=C_BG)
        self.top.transient(app.root)
        self.top.grab_set()
        w, h = 900, 720
        sw, sh = self.top.winfo_screenwidth(), self.top.winfo_screenheight()
        self.top.geometry("%dx%d+%d+%d" % (w, h, max(0, (sw - w) // 2),
                                           max(0, (sh - h) // 3)))
        self._build(saved)
        self.refresh()

    def _build(self, saved):
        head = tk.Frame(self.top, bg=C_DARK, height=52)
        head.pack(fill="x")
        head.pack_propagate(False)
        tk.Label(head, text="🎭 配置人数与身份 · 自定义模式", bg=C_DARK, fg=C_WHITE,
                 font=(UI_FONT, 14, "bold")).pack(side="left", padx=16)
        tk.Label(head, text="狼人 / 平民可重复，其它身份每个最多 1 个；神职最多 5 个",
                 bg=C_DARK, fg="#a9e5c4", font=(UI_FONT, 9)).pack(side="left")

        body = tk.Frame(self.top, bg=C_BG)
        body.pack(fill="both", expand=True, padx=14, pady=10)

        top = tk.Frame(body, bg=C_BG)
        top.pack(fill="x", pady=(0, 8))
        tk.Label(top, text="玩家人数：", bg=C_BG, fg=C_DARK,
                 font=(UI_FONT, 11, "bold")).pack(side="left")
        self.count = tk.IntVar(value=len(saved))
        sp = tk.Spinbox(top, from_=MIN_PLAYERS, to=MAX_PLAYERS, width=4,
                        textvariable=self.count, font=(UI_FONT, 12, "bold"),
                        command=self.on_count)
        sp.pack(side="left", padx=6)
        tk.Label(top, text="（%d~%d 人，含玩家；上帝 AI 不计入）"
                 % (MIN_PLAYERS, MAX_PLAYERS), bg=C_BG, fg=C_MUTED,
                 font=(UI_FONT, 9)).pack(side="left", padx=6)
        tk.Button(top, text="按人数重排（保持已有身份）", command=self.on_count,
                  bg=C_MID, fg=C_WHITE, relief="flat", padx=12, pady=3,
                  font=(UI_FONT, 9), cursor="hand2").pack(side="right")

        # 身份选择区（可滚动）
        wrap = tk.Frame(body, bg=C_BG)
        wrap.pack(fill="both", expand=True)
        canvas = tk.Canvas(wrap, bg=C_BG, highlightthickness=0, height=380)
        sbar = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        holder = tk.Frame(canvas, bg=C_BG)
        canvas.create_window((0, 0), window=holder, anchor="nw", width=840)
        canvas.configure(yscrollcommand=sbar.set)
        sbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        holder.bind("<Configure>",
                    lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

        self.vars = {}
        self.cells = {}
        for seat in range(1, MAX_PLAYERS + 1):
            role = saved[seat - 1] if seat <= len(saved) else ROLE_VILLAGER
            cell = tk.Frame(holder, bg="#ffffff", highlightbackground="#cfe9d8",
                            highlightthickness=1)
            cell.pack(fill="x", padx=6, pady=3)
            tk.Label(cell, text="%d 号席位" % seat, bg="#ffffff", fg=C_DARK, width=9,
                     anchor="w", font=(UI_FONT, 10, "bold")).pack(side="left", padx=(8, 4))
            var = tk.StringVar(value=role)
            cb = ttk.Combobox(cell, textvariable=var, state="readonly",
                              values=list(CUSTOM_ROLE_POOL), width=10,
                              font=(UI_FONT, 10))
            cb.pack(side="left", padx=4, pady=5)
            cb.bind("<<ComboboxSelected>>", lambda e: self.refresh())
            self.vars[seat] = var
            camp = tk.Label(cell, text="", bg="#ffffff", fg=C_MUTED, width=10,
                            anchor="w", font=(UI_FONT, 9))
            camp.pack(side="left", padx=4)
            desc = tk.Label(cell, text="", bg="#ffffff", fg=C_MUTED, anchor="w",
                            font=(UI_FONT, 8))
            desc.pack(side="left", padx=4)
            self.vars["lbl%d" % seat] = camp
            self.vars["desc%d" % seat] = desc
            self.cells[seat] = cell

        quick = tk.Frame(body, bg=C_BG)
        quick.pack(fill="x", pady=6)
        tk.Button(quick, text="🎲 随机分配当前身份", command=self.shuffle_roles,
                  bg="#b5179e", fg=C_WHITE, relief="flat", padx=12, pady=4,
                  font=(UI_FONT, 10, "bold"), cursor="hand2").pack(side="left")
        tk.Button(quick, text="一键填默认牌组", command=self.fill_default,
                  bg=C_MID, fg=C_WHITE, relief="flat", padx=12, pady=4,
                  font=(UI_FONT, 10), cursor="hand2").pack(side="left", padx=6)
        tk.Button(quick, text="其余全填平民", command=self.fill_villagers,
                  bg="#cfd8d3", fg="#233", relief="flat", padx=12, pady=4,
                  font=(UI_FONT, 10), cursor="hand2").pack(side="left", padx=6)
        tk.Label(quick, text="随机分配只打乱位置，身份组合不变",
                 bg=C_BG, fg=C_MUTED, font=(UI_FONT, 8)).pack(side="left", padx=8)

        self.stat_box = tk.LabelFrame(body, text="当前阵容", bg=C_BG, fg=C_DARK)
        self.stat_box.pack(fill="x", pady=(2, 8))
        self.lb_stat = tk.Label(self.stat_box, text="", bg=C_BG, fg=C_TEXT,
                                font=(UI_FONT, 11, "bold"), anchor="w", justify="left")
        self.lb_stat.pack(fill="x", padx=10, pady=(6, 2))
        self.lb_warn = tk.Label(self.stat_box, text="", bg=C_BG, fg="#b3261e",
                                font=(UI_FONT, 10), anchor="w", justify="left",
                                wraplength=840)
        self.lb_warn.pack(fill="x", padx=10)
        self.lb_hint = tk.Label(self.stat_box, text="", bg=C_BG, fg=C_MUTED,
                                font=(UI_FONT, 9), anchor="w", justify="left",
                                wraplength=840)
        self.lb_hint.pack(fill="x", padx=10, pady=(2, 8))

        bottom = tk.Frame(body, bg=C_BG)
        bottom.pack(fill="x")
        tk.Label(bottom, text="保存后自动切到自定义模式", bg=C_BG, fg=C_MUTED,
                 font=(UI_FONT, 9)).pack(side="left")
        tk.Button(bottom, text="保存并启用", command=self.save,
                  bg="#39d98a", fg="#08351f", relief="flat", padx=18, pady=6,
                  font=(UI_FONT, 11, "bold"), cursor="hand2").pack(side="right")
        tk.Button(bottom, text="取消", command=self.top.destroy,
                  bg="#cfd8d3", fg="#233", relief="flat", padx=14, pady=6,
                  font=(UI_FONT, 10), cursor="hand2").pack(side="right", padx=8)

    # ------------------------------------------------------------- 交互
    def on_count(self):
        try:
            n = int(self.count.get())
        except (TypeError, ValueError):
            n = MIN_PLAYERS
        n = max(MIN_PLAYERS, min(MAX_PLAYERS, n))
        self.count.set(n)
        self.refresh()
        return n

    def current_roles(self):
        n = max(MIN_PLAYERS, min(MAX_PLAYERS, int(self.count.get() or 10)))
        return [self.vars[s].get() for s in range(1, n + 1)]

    def refresh(self):
        n = max(MIN_PLAYERS, min(MAX_PLAYERS, int(self.count.get() or MIN_PLAYERS)))
        for seat in range(1, MAX_PLAYERS + 1):
            visible = seat <= n
            if visible:
                self.cells[seat].pack(fill="x", padx=6, pady=3)
            else:
                self.cells[seat].pack_forget()
            role = self.vars[seat].get()
            col = ROLE_COLORS.get(role, C_MID)
            self.vars["lbl%d" % seat].configure(
                text=("狼人阵营" if role in WOLF_ROLES else "好人阵营"), fg=col)
            self.vars["desc%d" % seat].configure(text=ROLE_BRIEF.get(role, ""))
        roles = self.current_roles()
        ok, msg, stat = validate_roles(roles)
        counts = {}
        for r in roles:
            counts[r] = counts.get(r, 0) + 1
        detail = "、".join("%s×%d" % (r, c) for r, c in counts.items())
        if ok:
            self.lb_stat.configure(
                text="共 %d 人：狼人阵营 %d 名 ｜ 神职 %d 名 ｜ 平民 %d 名　（%s）"
                     % (stat["players"], stat["wolves"], stat["gods"],
                        stat["villagers"], detail), fg=C_DARK)
            self.lb_warn.configure(text="✅ 配置合法")
            self.lb_hint.configure(text="平衡提示：" + balance_hint(stat))
        else:
            self.lb_stat.configure(text="当前选择（%d 人）：%s" % (n, detail), fg=C_TEXT)
            self.lb_warn.configure(text="❌ " + msg)
            self.lb_hint.configure(text="")
        return ok

    def fill_default(self):
        n = max(MIN_PLAYERS, min(MAX_PLAYERS, int(self.count.get() or 10)))
        deck = [ROLE_WOLF, ROLE_WOLF_KING, ROLE_WHITE_WOLF_KING, ROLE_VILLAGER,
                ROLE_SEER, ROLE_WITCH, ROLE_HUNTER, ROLE_GUARD, ROLE_IDIOT,
                ROLE_VILLAGER, ROLE_VILLAGER, ROLE_VILLAGER]
        for s in range(1, MAX_PLAYERS + 1):
            self.vars[s].set(deck[(s - 1) % len(deck)])
        self.refresh()

    def fill_villagers(self):
        n = max(MIN_PLAYERS, min(MAX_PLAYERS, int(self.count.get() or 10)))
        for s in range(1, n + 1):
            if self.vars[s].get() == ROLE_VILLAGER and s == 1:
                self.vars[s].set(ROLE_WOLF)
            else:
                self.vars[s].set(ROLE_VILLAGER)
        self.refresh()

    def shuffle_roles(self):
        """把当前已选定的身份在参战席位之间随机打乱（组合不变，只换位置）。"""
        n = max(MIN_PLAYERS, min(MAX_PLAYERS, int(self.count.get() or 10)))
        original = [self.vars[s].get() for s in range(1, n + 1)]
        picked = list(original)
        for _try in range(30):
            random.shuffle(picked)
            if picked != original:            # 尽量让位置真的变了
                break
        for idx, role in enumerate(picked, start=1):
            self.vars[idx].set(role)
        self.refresh()
        return picked

    def save(self):
        roles = self.current_roles()
        ok, msg, stat = validate_roles(roles)
        if not ok:
            messagebox.showwarning("配置不合法", msg, parent=self.top)
            return
        self.cfg["custom_roles"] = roles
        self.cfg["mode"] = "custom"
        if save_config(self.cfg):
            self.app.refresh_mode_ui()
            self.app.refresh_seats()
            self.app.log_system("身份配置已保存（%d 人）：%s ｜ %s"
                                % (len(roles), "、".join(roles), balance_hint(stat)))
            self.top.destroy()


# ==========================================================================
# API / 席位配置对话框
# ==========================================================================
class ConfigDialog(object):

    def __init__(self, app):
        self.app = app
        self.cfg = app.cfg
        self.top = tk.Toplevel(app.root)
        self.top.title("配置 API 与 AI 席位（%d 名玩家 + 上帝）" % MAX_PLAYERS)
        self.top.configure(bg=C_BG)
        self.top.transient(app.root)
        self.top.grab_set()
        w, h = 1100, 820
        sw, sh = self.top.winfo_screenwidth(), self.top.winfo_screenheight()
        self.top.geometry("%dx%d+%d+%d" % (min(w, sw - 60), min(h, sh - 80),
                                           max(0, (sw - w) // 2),
                                           max(0, (sh - h) // 3)))
        self.current = 1
        self._build()
        self._reload_tree(keep_selection=False)
        self._select(1)

    # ------------------------------------------------------------- 构建
    def _build(self):
        top = tk.Frame(self.top, bg=C_DARK, height=56)
        top.pack(fill="x")
        top.pack_propagate(False)
        tk.Label(top, text="⚙ 配置 AI 接口（%d 个玩家席位 + 上帝）" % MAX_PLAYERS,
                 bg=C_DARK, fg=C_WHITE,
                 font=(UI_FONT, 14, "bold")).pack(side="left", padx=16)
        tk.Label(top, text="名单固定显示全部席位；同一个人模型可重复占多个席位；"
                           "留空该席位无法发言。",
                 bg=C_DARK, fg="#a9e5c4", font=(UI_FONT, 9)).pack(side="left")

        body = tk.Frame(self.top, bg=C_BG)
        body.pack(fill="both", expand=True, padx=12, pady=10)

        # 预设
        pre = tk.LabelFrame(body, text="① 快速套用服务商预设", bg=C_BG, fg=C_DARK)
        pre.pack(fill="x", pady=(0, 8))
        self.preset = tk.StringVar(value="（选择后仅自动填写下方表单，不会直接生效）")
        cb = ttk.Combobox(pre, textvariable=self.preset, state="readonly", width=78,
                          values=["（选择后仅自动填写下方表单，不会直接生效）"] +
                          ["%s ｜ %s ｜ %s" % (n, u, m) for n, u, m in PROVIDER_PRESETS])
        cb.pack(side="left", padx=8, pady=8)
        cb.bind("<<ComboboxSelected>>", self._on_preset)
        tk.Label(pre, text="先选中下方表格里的席位 → 选预设 → 点【应用到选中席位】",
                 bg=C_BG, fg=C_MUTED, font=(UI_FONT, 9)).pack(side="left")

        # 表格：固定高度容纳全部席位 + 上帝，保证每个席位都看得到
        mid = tk.Frame(body, bg=C_BG)
        mid.pack(fill="both", expand=True)
        cols = ("seat", "role", "model", "base_url", "tokens")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings",
                                 height=MAX_PLAYERS + 1, selectmode="extended")
        heads = [("seat", "席位", 90), ("role", "阵营 / 状态", 150),
                 ("model", "模型", 300), ("base_url", "API 地址", 340),
                 ("tokens", "已消耗 tokens", 130)]
        for key, text, width in heads:
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, anchor="w")
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        # 表单
        form = tk.LabelFrame(body, text="② 编辑当前选中的席位", bg=C_BG, fg=C_DARK)
        form.pack(fill="x", pady=8)
        grid = tk.Frame(form, bg=C_BG)
        grid.pack(fill="x", padx=8, pady=6)

        self.var_name = tk.StringVar()
        self.var_url = tk.StringVar()
        self.var_model = tk.StringVar()
        self.var_key = tk.StringVar()
        self.var_temp = tk.StringVar(value="0.85")
        self.var_maxtok = tk.StringVar(value="700")
        self.var_timeout = tk.StringVar(value="120")
        self.var_json = tk.BooleanVar(value=False)

        self._field(grid, "显示名", self.var_name, 0, 0, 18)
        self._field(grid, "模型名", self.var_model, 0, 2, 44)
        self._field(grid, "API 地址（到 /v1）", self.var_url, 1, 0, 62)
        self._field(grid, "API Key", self.var_key, 2, 0, 62, show="•")
        self._field(grid, "温度", self.var_temp, 2, 2, 8)
        self._field(grid, "输出上限 max_tokens", self.var_maxtok, 3, 0, 10)
        self._field(grid, "超时(秒)", self.var_timeout, 3, 2, 10)
        ttk.Checkbutton(grid, text="请求 JSON 模式（部分服务商支持，失败可关闭）",
                        variable=self.var_json).grid(row=4, column=0, columnspan=3,
                                                     sticky="w", pady=4)
        self.var_thinking = tk.BooleanVar(value=False)
        ttk.Checkbutton(grid, text="思考模式（推理更强，但更慢更贵；DeepSeek V4/V4.1 用参数开启）",
                        variable=self.var_thinking).grid(row=5, column=0, columnspan=2,
                                                         sticky="w", pady=4)
        tk.Label(grid, text="不勾选时会显式发送 thinking=disabled，避免模型默认带思考、"
                            "把推理过程当台词输出",
                 bg=C_BG, fg=C_MUTED, font=(UI_FONT, 8)).grid(row=5, column=2,
                                                             sticky="w", padx=6)

        btns = tk.Frame(form, bg=C_BG)
        btns.pack(fill="x", padx=8, pady=(0, 8))
        tk.Button(btns, text="应用到选中席位", command=self._apply_fields,
                  bg=C_MID, fg=C_WHITE, relief="flat", padx=12, pady=5,
                  font=(UI_FONT, 10, "bold"), cursor="hand2").pack(side="left")
        tk.Button(btns, text="复制到全部席位", command=lambda: self._bulk("all"),
                  bg="#4a90d9", fg=C_WHITE, relief="flat", padx=12, pady=5,
                  font=(UI_FONT, 10), cursor="hand2").pack(side="left", padx=6)
        tk.Button(btns, text="只填充未配置的席位", command=lambda: self._bulk("empty"),
                  bg="#4a90d9", fg=C_WHITE, relief="flat", padx=12, pady=5,
                  font=(UI_FONT, 10), cursor="hand2").pack(side="left", padx=6)
        tk.Button(btns, text="🔌 测试连通性", command=self._test,
                  bg="#ff9f1c", fg="#3a2200", relief="flat", padx=12, pady=5,
                  font=(UI_FONT, 10, "bold"), cursor="hand2").pack(side="left", padx=6)
        self.lb_test = tk.Label(btns, text="", bg=C_BG, fg=C_MUTED, font=(UI_FONT, 9))
        self.lb_test.pack(side="left", padx=10)

        # 底部
        bottom = tk.Frame(body, bg=C_BG)
        bottom.pack(fill="x", pady=(4, 0))
        tk.Label(bottom,
                 text="上帝 AI 在第 12 行；经典模式用 11 个席位，自定义模式用前 10 个。",
                 bg=C_BG, fg=C_MUTED, font=(UI_FONT, 9)).pack(side="left")
        tk.Button(bottom, text="保存并关闭", command=self._save,
                  bg="#39d98a", fg="#08351f", relief="flat", padx=18, pady=6,
                  font=(UI_FONT, 11, "bold"), cursor="hand2").pack(side="right")
        tk.Button(bottom, text="取消", command=self.top.destroy,
                  bg="#cfd8d3", fg="#233", relief="flat", padx=14, pady=6,
                  font=(UI_FONT, 10), cursor="hand2").pack(side="right", padx=8)

    def _field(self, parent, label, var, row, col, width, show=None):
        tk.Label(parent, text=label, bg=C_BG, fg=C_TEXT,
                 font=(UI_FONT, 9)).grid(row=row, column=col, sticky="e", padx=(8, 4),
                                         pady=4)
        e = tk.Entry(parent, textvariable=var, width=width, font=(UI_FONT, 10),
                     show=show, relief="solid", bd=1)
        e.grid(row=row, column=col + 1, sticky="w", pady=4)
        return e

    # ------------------------------------------------------------- 数据
    def _seat_label(self, seat):
        if seat == JUDGE_ROW:
            return "上帝", "上帝 / 裁判 / 流程播报"
        roles = self.app.effective_roles()
        live = self.app.roles.get(seat)
        if live:
            return str(seat), "玩家席位 %d · %s" % (seat, live)
        if roles and seat <= len(roles):
            return str(seat), "玩家席位 %d · 预设 %s" % (seat, roles[seat - 1])
        if roles:
            return str(seat), "（本局只用到前 %d 个席位）" % len(roles)
        return str(seat), "玩家席位 %d（身份开局随机）" % seat

    def _tree_values(self, seat):
        if seat == JUDGE_ROW:
            c = self.cfg["judge"]
            label, role = "上帝", "上帝 / 裁判 / 流程播报"
            key = "上帝"
        else:
            c = self.cfg["seats"][str(seat)]
            label, role = self._seat_label(seat)
            key = seat_name(seat)
        if c.get("thinking"):
            role = role + " ｜ 🧠思考"
        return (label, role,
                c.get("model") or "（未配置）",
                c.get("base_url") or "（未配置）",
                fmt_num(self.app.meter.agent_total(key)))

    def _reload_tree(self, keep_selection=True):
        sel = self.tree.selection()
        for i in self.tree.get_children():
            self.tree.delete(i)
        for seat in list(range(1, MAX_PLAYERS + 1)) + [JUDGE_ROW]:
            iid = "S%d" % seat
            self.tree.insert("", "end", iid=iid, values=self._tree_values(seat))
        if keep_selection and sel:
            try:
                self.tree.selection_set(sel)
            except Exception:
                pass

    def _select(self, seat):
        self.current = seat
        iid = "S%d" % seat
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        self.tree.see(iid)
        self._load_fields(seat)

    def _on_select(self, _=None):
        sel = self.tree.selection()
        if not sel:
            return
        seat = int(sel[0][1:])
        if seat != self.current:
            self.current = seat
            self._load_fields(seat)

    def _sel_seats(self):
        sel = self.tree.selection() or ("S%d" % self.current,)
        return [int(i[1:]) for i in sel]

    def _config_of(self, seat):
        return self.cfg["judge"] if seat == JUDGE_ROW else self.cfg["seats"][str(seat)]

    def _load_fields(self, seat):
        c = self._config_of(seat)
        self.var_name.set(c.get("name")
                          or ("上帝" if seat == JUDGE_ROW else seat_name(seat)))
        self.var_url.set(c.get("base_url") or "")
        self.var_model.set(c.get("model") or "")
        self.var_key.set(c.get("api_key") or "")
        self.var_temp.set(str(c.get("temperature", 0.85)))
        self.var_maxtok.set(str(c.get("max_tokens", 700)))
        self.var_timeout.set(str(c.get("timeout", 120)))
        self.var_json.set(bool(c.get("json_mode", False)))
        self.var_thinking.set(bool(c.get("thinking", False)))

    def _on_preset(self, _=None):
        text = self.preset.get()
        for name, url, model in PROVIDER_PRESETS:
            if text.startswith(name + " ｜"):
                if url:
                    self.var_url.set(url)
                if model:
                    self.var_model.set(model)
                self.lb_test.configure(
                    text="已填入预设：%s，点【应用到选中席位】生效" % name, fg=C_MID)
                return

    # ------------------------------------------------------------- 应用
    def _fields_to_seat(self, seat):
        c = self._config_of(seat)
        c["name"] = (self.var_name.get().strip()
                     or ("上帝" if seat == JUDGE_ROW else seat_name(seat)))
        c["base_url"] = self.var_url.get().strip()
        c["model"] = self.var_model.get().strip()
        c["api_key"] = self.var_key.get().strip()
        try:
            c["temperature"] = float(self.var_temp.get())
        except ValueError:
            c["temperature"] = 0.85
        try:
            c["max_tokens"] = int(float(self.var_maxtok.get()))
        except ValueError:
            c["max_tokens"] = 700
        try:
            c["timeout"] = int(float(self.var_timeout.get()))
        except ValueError:
            c["timeout"] = 120
        c["json_mode"] = bool(self.var_json.get())
        c["thinking"] = bool(self.var_thinking.get())

    def _apply_fields(self):
        seats = self._sel_seats()
        for seat in seats:
            self._fields_to_seat(seat)
        self._reload_tree()
        self.lb_test.configure(
            text="已应用到：" + "、".join(self._label_of(s) for s in seats), fg=C_MID)

    def _label_of(self, seat):
        return "上帝" if seat == JUDGE_ROW else ("%d号" % seat)

    def _bulk(self, mode):
        seats = self._sel_seats()
        src = seats[0]
        self._fields_to_seat(src)
        c = self._config_of(src)
        targets = []
        for seat in list(range(1, MAX_PLAYERS + 1)) + [JUDGE_ROW]:
            tgt = self._config_of(seat)
            if mode == "empty" and tgt.get("base_url") and tgt.get("model"):
                continue
            tgt.update({k: v for k, v in c.items() if k != "name"})
            tgt["name"] = "上帝" if seat == JUDGE_ROW else seat_name(seat)
            targets.append(self._label_of(seat))
        self._reload_tree()
        self.lb_test.configure(text="已复制到：" + "、".join(targets), fg=C_MID)

    def _test(self):
        self._fields_to_seat(self.current)
        c = dict(self._config_of(self.current))
        c["name"] = "测试"
        self.lb_test.configure(text="正在测试 %s …" % (c.get("model") or "未填模型"),
                               fg="#8a6d00")
        self.top.update_idletasks()

        def work():
            ok, msg = test_connection(c, self.app.meter)
            self.top.after(0, lambda: self._test_done(ok, msg))
        threading.Thread(target=work, daemon=True).start()

    def _test_done(self, ok, msg):
        self.lb_test.configure(text=("✅ " if ok else "❌ ") + msg[:120],
                               fg=(C_MID if ok else "#b3261e"))
        self.app.refresh_tokens()
        self._reload_tree()

    def _save(self):
        sel = self.tree.selection() or ("S%d" % self.current,)
        if sel:
            self._fields_to_seat(int(sel[0][1:]))
        if save_config(self.cfg):
            self.app.refresh_seats()
            self.app.refresh_tokens()
            self.app.log_system("API 配置已保存（%d 个席位已填写地址）。"
                                % sum(1 for s in range(1, MAX_PLAYERS + 1)
                                      if self.cfg["seats"][str(s)].get("base_url")))
            self.top.destroy()


# ==========================================================================
# 游戏设置对话框（9 项，默认全关，遗言默认开）
# ==========================================================================
SETTING_ITEMS = [
    ("wolf_explode", "允许普通狼人白天自爆", "关闭后只有狼王 / 白狼王能自爆"),
    ("last_words", "启用遗言机制", "第一晚出局者（除毒杀）有遗言；第二晚起夜间无遗言；"
                                  "白天出局（除自爆）都有遗言"),
    ("guard_double", "启用奶穿（同守同救死亡）",
     "开启后：守卫守护 + 女巫解药同时作用于同一人时该玩家依然出局"),
    ("reveal_death", "死亡时公开身份与死因", "关闭后公开播报只说「X号 出局」，"
                                            "身份死因只显示在上帝视角斜体行"),
    ("witch_know_kill", "女巫每晚都知道被刀的人", "关闭后只有第一晚告知刀口"),
    ("witch_two_potions", "女巫同一晚可用两瓶药", "关闭后解药与毒药不能同一晚同时使用"),
    ("sheriff", "启用警长机制", "开启后第一个白天进行警长竞选，警长票 1.5 倍并决定发言顺序"),
    ("allow_abstain", "投票允许弃票", "开启后放逐投票与警长投票都可以弃票"
                                     "（弃票不计入任何候选人，与最高票持平则进入重投）"),
]


class SettingsDialog(object):

    def __init__(self, app):
        self.app = app
        self.cfg = app.cfg
        self.st = normalize_settings(self.cfg.get("settings"))
        self.top = tk.Toplevel(app.root)
        self.top.title("游戏设置")
        self.top.configure(bg=C_BG)
        self.top.transient(app.root)
        self.top.grab_set()
        w, h = 760, 620
        sw, sh = self.top.winfo_screenwidth(), self.top.winfo_screenheight()
        self.top.geometry("%dx%d+%d+%d" % (w, h, max(0, (sw - w) // 2),
                                           max(0, (sh - h) // 3)))
        self.vars = {}
        self._build()
        self.refresh_hint()

    def _build(self):
        head = tk.Frame(self.top, bg=C_DARK, height=52)
        head.pack(fill="x")
        head.pack_propagate(False)
        tk.Label(head, text="🛠 游戏设置", bg=C_DARK, fg=C_WHITE,
                 font=(UI_FONT, 14, "bold")).pack(side="left", padx=16)
        tk.Label(head, text="默认全部开启（仅「死亡时公开身份与死因」默认关闭）；"
                            "对局开始后自动锁定",
                 bg=C_DARK, fg="#a9e5c4", font=(UI_FONT, 9)).pack(side="left")

        body = tk.Frame(self.top, bg=C_BG)
        body.pack(fill="both", expand=True, padx=14, pady=12)

        for key, title, desc in SETTING_ITEMS:
            row = tk.Frame(body, bg="#ffffff", highlightbackground="#cfe9d8",
                           highlightthickness=1)
            row.pack(fill="x", pady=3)
            var = tk.BooleanVar(value=bool(self.st.get(key)))
            self.vars[key] = var
            ttk.Checkbutton(row, text=title, variable=var,
                            command=self.refresh_hint).pack(side="left", padx=(10, 6),
                                                            pady=6)
            tk.Label(row, text=desc, bg="#ffffff", fg=C_MUTED, anchor="w",
                     font=(UI_FONT, 8), wraplength=560).pack(side="left", padx=6)

        # 狼人胜利规则
        box = tk.Frame(body, bg="#ffffff", highlightbackground="#cfe9d8",
                       highlightthickness=1)
        box.pack(fill="x", pady=3)
        tk.Label(box, text="狼人胜利规则", bg="#ffffff", fg=C_TEXT,
                 font=(UI_FONT, 10, "bold")).pack(side="left", padx=(10, 6), pady=6)
        self.win_mode = tk.StringVar(value=self.st.get("wolf_win_mode", "side"))
        ttk.Radiobutton(box, text="屠边（平民或神职全灭即胜，标准）", value="side",
                        variable=self.win_mode).pack(side="left", padx=6)
        ttk.Radiobutton(box, text="屠城（好人全灭才胜）", value="all",
                        variable=self.win_mode).pack(side="left", padx=6)

        # 上下文条数
        box2 = tk.Frame(body, bg="#ffffff", highlightbackground="#cfe9d8",
                        highlightthickness=1)
        box2.pack(fill="x", pady=3)
        tk.Label(box2, text="拼接进提示词的最近发言条数", bg="#ffffff", fg=C_TEXT,
                 font=(UI_FONT, 10, "bold")).pack(side="left", padx=(10, 6), pady=6)
        self.ctx = tk.IntVar(value=int(self.st.get("context_lines", 60)))
        for n in (20, 30, 60):
            ttk.Radiobutton(box2, text="%d 条" % n, value=n,
                            variable=self.ctx).pack(side="left", padx=6)
        tk.Label(box2, text="（越少越省 token，但 AI 更容易「失忆」）", bg="#ffffff",
                 fg=C_MUTED, font=(UI_FONT, 8)).pack(side="left", padx=8)

        self.lb_hint = tk.Label(body, text="", bg=C_BG, fg=C_DARK, anchor="w",
                                justify="left", font=(UI_FONT, 9), wraplength=700)
        self.lb_hint.pack(fill="x", pady=(10, 0))

        bottom = tk.Frame(body, bg=C_BG)
        bottom.pack(side="bottom", fill="x", pady=(10, 0))
        tk.Button(bottom, text="全部关闭", command=self.all_off,
                  bg="#cfd8d3", fg="#233", relief="flat", padx=14, pady=5,
                  font=(UI_FONT, 10), cursor="hand2").pack(side="left")
        tk.Button(bottom, text="保存", command=self.save,
                  bg="#39d98a", fg="#08351f", relief="flat", padx=20, pady=6,
                  font=(UI_FONT, 11, "bold"), cursor="hand2").pack(side="right")
        tk.Button(bottom, text="取消", command=self.top.destroy,
                  bg="#cfd8d3", fg="#233", relief="flat", padx=14, pady=6,
                  font=(UI_FONT, 10), cursor="hand2").pack(side="right", padx=8)

    def all_off(self):
        for var in self.vars.values():
            var.set(False)
        self.refresh_hint()

    def refresh_hint(self):
        on = [t for k, t, _d in SETTING_ITEMS if self.vars[k].get()]
        mode = "屠边" if self.win_mode.get() == "side" else "屠城"
        self.lb_hint.configure(
            text="当前开启：%s\n狼人胜利规则：%s　｜　上下文：%d 条"
                 % ("、".join(on) if on else "无（均为默认关闭）",
                    mode, int(self.ctx.get())))

    def save(self):
        st = dict(self.st)
        for key, _t, _d in SETTING_ITEMS:
            st[key] = bool(self.vars[key].get())
        st["wolf_win_mode"] = self.win_mode.get()
        st["context_lines"] = int(self.ctx.get())
        self.cfg["settings"] = normalize_settings(st)
        if save_config(self.cfg):
            self.top.destroy()


# ==========================================================================
def main():
    root = tk.Tk()
    app = WerewolfApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
