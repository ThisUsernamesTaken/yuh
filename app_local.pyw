"""
BTC Bias Engine — Dashboard
Double-click to launch. First run asks for credentials.
After that, auto-starts the engine and shows a live trading dashboard.
"""
import os, sys, re, math, shutil, threading, subprocess, time, json
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from collections import deque
from pathlib import Path
from datetime import datetime

# ── Path resolution: frozen exe vs source ──
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent

DATA_DIR  = APP_DIR / "data"
CRED_DIR  = APP_DIR / "credentials"
CRED_FILE = CRED_DIR / "kalshi.env"
CONFIG_PATH = APP_DIR / "user_config.py"
LOG_PATH  = DATA_DIR / "engine_history.log"
ENGINE_PY = APP_DIR / "main.py"

# ── Theme ──
BG = "#0d1117"; BG2 = "#161b22"; BG3 = "#1c2333"; BORDER = "#30363d"
TEXT = "#e6edf3"; DIM = "#484f58"; ACCENT = "#58a6ff"
GREEN = "#3fb950"; RED = "#f85149"; ORANGE = "#f0883e"
PURPLE = "#d2a8ff"; CYAN = "#39d2c0"; YELLOW = "#e3b341"
NO_WIN = 0x08000000

# Default config matching this PC's proven values
DEFAULT_CONFIG = (
    'ENGINE_DIR = r"C:\\Trading\\btc-bias-engine"\n'
    "SIZING_BALANCE_FRACTION = 0.30\n"
    "SIZING_MAX_DOLLARS = 50.00\n"
    "MIN_BALANCE_TO_TRADE = 2.00\n"
    "DAILY_LOSS_LIMIT = 15.00\n"
    "TAKE_PROFIT_CENTS = 8\n"
    "MIN_ENTRY_CENTS = 40\n"
    "MIN_ENTRY_CENTS_NO = 40\n"
    "MAX_ENTRY_CENTS = 55\n"
    "MIN_DIVERGENCE = 0.01\n"
    "MIN_SMART_WALLETS = 1\n"
    "MIN_FLOW_CONVICTION = 0.50\n"
    "STOP_LOSS_CENTS = 8\n"
    "STOP_LOSS_CENTS_PRIMARY_WIDE = 8\n"
    "STOP_LOSS_CENTS_HIGH_ENTRY = 5\n"
    "HIGH_ENTRY_STOP_THRESHOLD = 83\n"
    "STOP_GRACE_PERIOD_S = 15\n"
    "MIMIC_ENABLED = False\n"
    "MIMIC_MIN_WALLET_WR = 0.70\n"
    "DIVERGENCE_LADDER_ENABLED = True\n"
    "BLOCKED_HOURS = set()\n"
    "TA_FORCED_ENABLED = True\n"
    "TA_INVERSION_ENABLED = True\n"
    "MTF_ENABLED = True\n"
    "MTF_SHADOW_MODE = False\n"
    "MTF_MIN_CONFLUENCE = 0.3\n"
    "MTF_SIZE_MULTIPLIER_HIGH = 1.5\n"
    'MTF_TIMEFRAMES = ["1m", "5m", "15m", "1h"]\n'
    'PRICE_FEED_SYMBOL = "btcusdt"\n'
    "PRICE_FEED_WS_TIMEOUT_S = 30.0\n"
    "PAPER_TRADING = False\n"
    "PAPER_STARTING_BALANCE = 100.0\n"
    "PAPER_SLIPPAGE_CENTS = 1\n"
    "MAX_TRADES_PER_WINDOW = 6\n"
    "TRADE_COOLDOWN_SECONDS = 10\n"
    "MIN_MINUTES_REMAINING = 2.0\n"
    "MAX_MINUTES_REMAINING = 15.0\n"
    "SELL_LADDER_ENABLED = True\n"
    "FLIP_REENTRY_ENABLED = True\n"
)


class AnimBar(tk.Frame):
    """Animated horizontal bar gauge."""
    def __init__(self, p, w=180, h=8):
        super().__init__(p, bg=BG2)
        self._c = tk.Canvas(self, width=w, height=h, bg=BG, highlightthickness=0)
        self._c.pack()
        self._bw = w; self._bh = h; self._val = 0.5; self._tgt = 0.5; self._clr = DIM
        self.after(80, self._tick)

    def set(self, v, c=None):
        self._tgt = max(0, min(1, v))
        if c: self._clr = c

    def _tick(self):
        self._val += (self._tgt - self._val) * 0.18
        c = self._c; w = self._bw; h = self._bh
        c.delete("all")
        c.create_rectangle(0, 0, w, h, fill=BG, outline=BORDER)
        bw = int(self._val * w)
        if bw > 1:
            c.create_rectangle(0, 0, bw, h, fill=self._clr, outline="")
        c.create_line(w // 2, 0, w // 2, h, fill=DIM)
        self.after(30, self._tick)


class MidChart(tk.Frame):
    """Live contract mid-price chart with entry/TP lines."""
    def __init__(self, p, w=420, h=120):
        super().__init__(p, bg=BG2)
        self._c = tk.Canvas(self, width=w, height=h, bg=BG, highlightthickness=0)
        self._c.pack(padx=2, pady=2)
        self._cw = w; self._ch = h; self._data = deque(maxlen=90); self._entry = 0; self._tps = []

    def add(self, mid):
        self._data.append(mid)
        self._draw()

    def set_entry(self, p):
        self._entry = p

    def set_tps(self, tps):
        self._tps = tps

    def _draw(self):
        c = self._c; c.delete("all")
        if len(self._data) < 2:
            c.create_text(self._cw // 2, self._ch // 2, text="waiting for data...",
                         fill=DIM, font=("Consolas", 9))
            return
        data = list(self._data)
        lo = max(0, min(data) - 5); hi = min(100, max(data) + 5); rng = hi - lo or 1
        w = self._cw; h = self._ch; step = w / max(len(data) - 1, 1)

        # Grid lines
        for pct in [25, 50, 75]:
            if lo <= pct <= hi:
                y = h - int((pct - lo) / rng * h)
                c.create_line(0, y, w, y, fill=BORDER, dash=(2, 4))
                c.create_text(w - 3, y - 8, text=f"{pct}c", fill=DIM,
                             font=("Consolas", 7), anchor="e")

        # 50c center line
        if lo <= 50 <= hi:
            y50 = h - int((50 - lo) / rng * h)
            c.create_line(0, y50, w, y50, fill=ORANGE, dash=(3, 3))

        # Dead zone shading (47-53c)
        for edge in [47, 53]:
            if lo <= edge <= hi:
                ye = h - int((edge - lo) / rng * h)
                c.create_line(0, ye, w, ye, fill="#2d1f00", dash=(1, 3))

        # Entry line
        if self._entry and lo <= self._entry <= hi:
            ye = h - int((self._entry - lo) / rng * h)
            c.create_line(0, ye, w, ye, fill=PURPLE, dash=(5, 3))
            c.create_text(4, ye - 8, text=f"entry {self._entry}c", fill=PURPLE,
                         font=("Consolas", 7, "bold"), anchor="w")

        # TP lines
        tp_colors = [GREEN, CYAN, YELLOW]
        for i, tp in enumerate(self._tps):
            if lo <= tp <= hi:
                yt = h - int((tp - lo) / rng * h)
                clr = tp_colors[i % len(tp_colors)]
                c.create_line(0, yt, w, yt, fill=clr, dash=(2, 2))
                c.create_text(w - 4, yt + 8, text=f"tp {tp}c", fill=clr,
                             font=("Consolas", 7), anchor="e")

        # Price line
        pts = [(int(i * step), h - int((v - lo) / rng * h)) for i, v in enumerate(data)]
        lv = data[-1]
        lc = GREEN if lv > 50 else RED if lv < 50 else TEXT
        for i in range(len(pts) - 1):
            c.create_line(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1],
                         fill=lc, width=2)
        if pts:
            lx, ly = pts[-1]
            c.create_oval(lx - 4, ly - 4, lx + 4, ly + 4, fill=lc, outline="")
            c.create_text(lx - 8, ly - 12, text=f"{lv}c", fill=lc,
                         font=("Consolas", 10, "bold"), anchor="e")


class PnLChart(tk.Frame):
    """Cumulative P&L chart."""
    def __init__(self, p, w=420, h=120):
        super().__init__(p, bg=BG2)
        self._c = tk.Canvas(self, width=w, height=h, bg=BG, highlightthickness=0)
        self._c.pack(padx=2, pady=2)
        self._cw = w; self._ch = h; self._data = deque(maxlen=90)

    def add(self, v):
        self._data.append(v)
        self._draw()

    def _draw(self):
        c = self._c; c.delete("all")
        if len(self._data) < 2:
            c.create_text(self._cw // 2, self._ch // 2, text="no trades yet",
                         fill=DIM, font=("Consolas", 9))
            return
        data = list(self._data)
        lo = min(data) - 1; hi = max(data) + 1; rng = hi - lo or 1
        w = self._cw; h = self._ch; step = w / max(len(data) - 1, 1)

        # Zero line
        if lo <= 0 <= hi:
            y0 = h - int(-lo / rng * h)
            c.create_line(0, y0, w, y0, fill=DIM, dash=(2, 4))
            c.create_text(4, y0 - 8, text="$0", fill=DIM, font=("Consolas", 7), anchor="w")

        # Area fill
        pts = [(int(i * step), h - int((v - lo) / rng * h)) for i, v in enumerate(data)]
        clr = GREEN if data[-1] >= 0 else RED
        fill_clr = "#0a2e1a" if data[-1] >= 0 else "#2e0a0a"
        if len(pts) >= 2:
            y_base = h - int(-lo / rng * h) if lo <= 0 <= hi else h
            fill_pts = pts + [(pts[-1][0], y_base), (pts[0][0], y_base)]
            c.create_polygon(fill_pts, fill=fill_clr, outline="")

        for i in range(len(pts) - 1):
            c.create_line(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1],
                         fill=clr, width=2)
        if pts:
            lx, ly = pts[-1]
            c.create_oval(lx - 4, ly - 4, lx + 4, ly + 4, fill=clr, outline="")
            c.create_text(lx - 8, ly - 12, text=f"${data[-1]:+.2f}", fill=clr,
                         font=("Consolas", 10, "bold"), anchor="e")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("BTC Bias Engine")
        self.geometry("1080x820")
        self.configure(bg=BG)
        self.minsize(800, 600)
        self.resizable(True, True)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        CRED_DIR.mkdir(parents=True, exist_ok=True)
        if not CONFIG_PATH.exists():
            ex = APP_DIR / "user_config.example.py"
            if not ex.exists() and getattr(sys, "_MEIPASS", None):
                ex = Path(sys._MEIPASS) / "user_config.example.py"
            if ex.exists():
                shutil.copy2(ex, CONFIG_PATH)
            else:
                CONFIG_PATH.write_text(DEFAULT_CONFIG, encoding="utf-8")

        # State
        self._proc = None; self._last_sz = 0; self._pending = deque()
        self._reading = False; self._phase = 0; self._nssm_mode = False
        self._balance = 0.0; self._pnl = 0.0; self._cum_pnl = 0.0
        self._wins = 0; self._losses = 0; self._skips = 0
        self._pos = ""; self._mid = 50; self._ta_conf = 0; self._ta_rsi = 50
        self._mtf_score = 0.0; self._mtf_regime = ""; self._conv = 0; self._conv_r = ""
        self._flow_p = 0.5; self._flow_d = ""; self._mom = ""; self._whale = ""
        self._window = ""; self._burst = False; self._ladder = False; self._entry_px = 0
        self._sizing_pct = 0

        # First launch
        if not CRED_FILE.exists():
            self._first_launch()

        self._build()
        self.after(500, self._auto_start)
        self._poll()

    # ── First Launch ──

    def _first_launch(self):
        win = tk.Toplevel(self)
        win.title("First Time Setup")
        win.geometry("520x380")
        win.configure(bg=BG)
        win.transient(self)
        win.grab_set()

        tk.Label(win, text="BTC Bias Engine", font=("Consolas", 16, "bold"),
                 fg=ACCENT, bg=BG).pack(pady=(25, 3))
        tk.Label(win, text="connect your Kalshi account to start trading\nget credentials from kalshi.com/account/api",
                 font=("Consolas", 8), fg=DIM, bg=BG).pack(pady=(0, 15))

        f = tk.Frame(win, bg=BG2, padx=25, pady=20)
        f.pack(fill="x", padx=25)

        tk.Label(f, text="API Key (UUID)", font=("Consolas", 9), fg=DIM, bg=BG2).pack(anchor="w")
        api_var = tk.StringVar()
        ttk.Entry(f, textvariable=api_var, width=48, font=("Consolas", 10)).pack(fill="x", pady=(2, 10))

        tk.Label(f, text="RSA Private Key (.pem file)", font=("Consolas", 9), fg=DIM, bg=BG2).pack(anchor="w")
        pem_var = tk.StringVar()
        pf = tk.Frame(f, bg=BG2)
        pf.pack(fill="x", pady=(2, 12))
        ttk.Entry(pf, textvariable=pem_var, width=38, font=("Consolas", 10)).pack(side="left", fill="x", expand=True)
        ttk.Button(pf, text="Browse", command=lambda: pem_var.set(
            filedialog.askopenfilename(filetypes=[("PEM", "*.pem"), ("All", "*.*")])
        )).pack(side="right", padx=(8, 0))

        mode_var = tk.StringVar(value="live")
        mf = tk.Frame(f, bg=BG2)
        mf.pack(fill="x", pady=(0, 12))
        tk.Label(mf, text="Mode:", font=("Consolas", 9), fg=DIM, bg=BG2).pack(side="left", padx=(0, 10))
        ttk.Radiobutton(mf, text="Demo (paper)", variable=mode_var, value="demo").pack(side="left", padx=(0, 15))
        ttk.Radiobutton(mf, text="Live (real money)", variable=mode_var, value="live").pack(side="left")

        def save():
            api = api_var.get().strip()
            pem = pem_var.get().strip()
            if not api:
                messagebox.showerror("Missing", "Enter your API key")
                return
            if not pem or not Path(pem).exists():
                messagebox.showerror("Missing", "Select a valid .pem file")
                return
            dest = CRED_DIR / "kalshi_key.pem"
            shutil.copy2(pem, dest)
            demo = mode_var.get() == "demo"
            CRED_FILE.write_text(
                f"KALSHI_API_KEY={api}\n"
                f"KALSHI_PRIVATE_KEY_PATH={str(dest).replace(chr(92), '/')}\n"
                f"KALSHI_DEMO={'true' if demo else 'false'}\n"
                f"EXECUTE_TRADES={'false' if demo else 'true'}\n",
                encoding="utf-8")
            os.environ["KALSHI_API_KEY"] = api
            os.environ["KALSHI_PRIVATE_KEY_PATH"] = str(dest)
            os.environ["KALSHI_DEMO"] = "true" if demo else "false"
            os.environ["EXECUTE_TRADES"] = "false" if demo else "true"
            win.destroy()

        tk.Button(f, text="Save & Start Trading", font=("Consolas", 11, "bold"),
                  fg="white", bg=GREEN, relief="flat", padx=20, pady=6,
                  command=save).pack(pady=(5, 0))

        self.wait_window(win)

    # ── Engine Control ──

    def _auto_start(self):
        if not CRED_FILE.exists():
            return
        for line in CRED_FILE.read_text(encoding="utf-8").split("\n"):
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip()
        try:
            result = subprocess.run(["nssm", "status", "BTCBiasEngine"],
                capture_output=True, text=True, timeout=3, creationflags=NO_WIN)
            if "SERVICE_RUNNING" in result.stdout:
                self._nssm_mode = True
                self._log_add("NSSM service detected — dashboard monitoring only", "info")
                return
        except Exception:
            pass
        self._nssm_mode = False
        self._start_engine()

    def _start_engine(self):
        if self._proc and self._proc.poll() is None:
            return
        # Route engine stderr to a crash log so silent failures are visible
        err_log = DATA_DIR / "engine_stderr.log"
        err_file = open(err_log, "a", encoding="utf-8")
        if getattr(sys, "frozen", False):
            # PyInstaller exe — relaunch with --engine flag
            self._proc = subprocess.Popen(
                [sys.executable, "--engine"], cwd=str(APP_DIR),
                stdout=err_file, stderr=err_file,
                creationflags=NO_WIN | subprocess.CREATE_NEW_PROCESS_GROUP,
                env={**os.environ})
            self._log_add("Engine started (exe --engine mode)", "info")
        elif ENGINE_PY.exists():
            self._proc = subprocess.Popen(
                [sys.executable, str(ENGINE_PY)], cwd=str(APP_DIR),
                stdout=err_file, stderr=err_file,
                creationflags=NO_WIN | subprocess.CREATE_NEW_PROCESS_GROUP,
                env={**os.environ})
            self._log_add("Engine started (Python mode)", "info")
        else:
            try:
                subprocess.run(["nssm", "start", "BTCBiasEngine"],
                    capture_output=True, creationflags=NO_WIN)
                self._log_add("Engine started (NSSM mode)", "info")
            except Exception:
                self._log_add("Failed to start engine", "loss")

    def _stop_engine(self):
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            self._proc = None
        try:
            subprocess.run(["nssm", "stop", "BTCBiasEngine"],
                capture_output=True, creationflags=NO_WIN)
        except Exception:
            pass

    def _on_close(self):
        if self._proc and self._proc.poll() is None:
            if messagebox.askyesno("Quit", "Stop the trading engine too?"):
                self._stop_engine()
        self.destroy()

    # ── Build UI ──

    def _build(self):
        # Header
        hdr = tk.Frame(self, bg=BG)
        hdr.pack(fill="x", padx=14, pady=(10, 0))
        tk.Label(hdr, text="BTC BIAS ENGINE", font=("Consolas", 16, "bold"),
                 fg=ACCENT, bg=BG).pack(side="left")

        self._dot = tk.Canvas(hdr, width=16, height=16, bg=BG, highlightthickness=0)
        self._dot.pack(side="right", padx=5)
        self._stat_lbl = tk.Label(hdr, text="STARTING", font=("Consolas", 9, "bold"),
                                   fg=ORANGE, bg=BG)
        self._stat_lbl.pack(side="right")

        ctrl = tk.Frame(hdr, bg=BG)
        ctrl.pack(side="right", padx=20)
        for txt, clr, cmd in [
            ("Stop", RED, self._stop_engine),
            ("Restart", ORANGE, lambda: [self._stop_engine(), self.after(3000, self._start_engine)]),
            ("Settings", ACCENT, self._open_settings),
        ]:
            tk.Button(ctrl, text=txt, font=("Consolas", 8, "bold"), fg=BG, bg=clr,
                      relief="flat", padx=10, command=cmd).pack(side="left", padx=3)

        # Big numbers row
        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=14, pady=(8, 2))

        self._bal_l = tk.Label(top, text="$0.00", font=("Consolas", 28, "bold"), fg=TEXT, bg=BG)
        self._bal_l.pack(side="left")
        self._pnl_l = tk.Label(top, text="", font=("Consolas", 16, "bold"), fg=DIM, bg=BG)
        self._pnl_l.pack(side="left", padx=14, pady=(8, 0))
        self._wl_l = tk.Label(top, text="", font=("Consolas", 10), fg=DIM, bg=BG)
        self._wl_l.pack(side="left", padx=10, pady=(12, 0))
        self._skip_l = tk.Label(top, text="", font=("Consolas", 8), fg=DIM, bg=BG)
        self._skip_l.pack(side="left", padx=8, pady=(14, 0))
        self._pos_l = tk.Label(top, text="", font=("Consolas", 10, "bold"), fg=PURPLE, bg=BG)
        self._pos_l.pack(side="right")
        self._win_l = tk.Label(top, text="", font=("Consolas", 8), fg=DIM, bg=BG)
        self._win_l.pack(side="right", padx=10)

        # Charts
        ch = tk.Frame(self, bg=BG)
        ch.pack(fill="x", padx=14, pady=4)
        ch.columnconfigure(0, weight=1)
        ch.columnconfigure(1, weight=1)

        cf = tk.LabelFrame(ch, text=" contract price ", font=("Consolas", 8, "bold"),
                           fg=CYAN, bg=BG2, highlightbackground=BORDER, highlightthickness=1)
        cf.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        self._mid_chart = MidChart(cf, w=420, h=120)
        self._mid_chart.pack(padx=4, pady=4, fill="both", expand=True)

        pf = tk.LabelFrame(ch, text=" cumulative P&L ", font=("Consolas", 8, "bold"),
                           fg=GREEN, bg=BG2, highlightbackground=BORDER, highlightthickness=1)
        pf.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        self._pnl_chart = PnLChart(pf, w=420, h=120)
        self._pnl_chart.pack(padx=4, pady=4, fill="both", expand=True)

        # Algorithm panel
        sig = tk.LabelFrame(self, text=" algorithm ", font=("Consolas", 8, "bold"),
                            fg=CYAN, bg=BG2, highlightbackground=BORDER, highlightthickness=1)
        sig.pack(fill="x", padx=14, pady=4)
        sig.columnconfigure(0, weight=1)
        sig.columnconfigure(1, weight=1)

        left = tk.Frame(sig, bg=BG2)
        left.grid(row=0, column=0, sticky="nsew", padx=10, pady=6)

        dr = tk.Frame(left, bg=BG2)
        dr.pack(fill="x")
        self._dir_l = tk.Label(dr, text="--", font=("Consolas", 22, "bold"), fg=DIM, bg=BG2)
        self._dir_l.pack(side="left", padx=(10, 18))
        cc = tk.Frame(dr, bg=BG2)
        cc.pack(side="left")
        self._conv_l = tk.Label(cc, text="0 pts", font=("Consolas", 13, "bold"), fg=DIM, bg=BG2)
        self._conv_l.pack(anchor="w")
        self._conv_d = tk.Label(cc, text="", font=("Consolas", 7), fg=DIM, bg=BG2, wraplength=220)
        self._conv_d.pack(anchor="w")
        self._sizing_l = tk.Label(cc, text="", font=("Consolas", 7), fg=DIM, bg=BG2)
        self._sizing_l.pack(anchor="w")

        br = tk.Frame(left, bg=BG2)
        br.pack(fill="x", padx=10, pady=(6, 0))
        self._badges = {}
        for t, k, c in [("BURST", "burst", YELLOW), ("LADDER", "ladder", PURPLE),
                         ("MTF", "mtf", CYAN), ("WHALE", "whale", ORANGE),
                         ("FLIP", "flip", GREEN), ("SKIP", "skip", RED)]:
            b = tk.Label(br, text=f" {t} ", font=("Consolas", 7, "bold"),
                        fg=BG, bg=DIM, padx=4, pady=1)
            b.pack(side="left", padx=2)
            self._badges[k] = (b, c)

        right = tk.Frame(sig, bg=BG2)
        right.grid(row=0, column=1, sticky="nsew", padx=10, pady=6)
        self._bars = {}
        for label, key in [("Lean", "lean"), ("TA", "ta"), ("MTF", "mtf"),
                           ("Flow", "flow"), ("Mom", "mom")]:
            row = tk.Frame(right, bg=BG2)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=label, font=("Consolas", 8), fg=DIM, bg=BG2,
                    width=5, anchor="e").pack(side="left")
            bar = AnimBar(row, w=180, h=8)
            bar.pack(side="left", padx=6)
            vl = tk.Label(row, text="--", font=("Consolas", 8, "bold"),
                         fg=TEXT, bg=BG2, width=22, anchor="w")
            vl.pack(side="left")
            self._bars[key] = (bar, vl)

        # Live feed
        lf = tk.LabelFrame(self, text=" live feed ", font=("Consolas", 8, "bold"),
                           fg=ACCENT, bg=BG2, highlightbackground=BORDER, highlightthickness=1)
        lf.pack(fill="both", expand=True, padx=14, pady=(4, 10))

        self._log = tk.Text(lf, bg=BG, fg=TEXT, font=("Consolas", 9), wrap="word",
                           highlightthickness=0, borderwidth=0, state="disabled",
                           insertbackground=TEXT)
        scroll = ttk.Scrollbar(lf, orient="vertical", command=self._log.yview)
        self._log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self._log.pack(fill="both", expand=True, padx=5, pady=5)

        for t, c in [("win", GREEN), ("loss", RED), ("entry", ACCENT), ("info", DIM),
                     ("mtf", ORANGE), ("whale", CYAN), ("burst", YELLOW),
                     ("ladder", PURPLE), ("flow", CYAN), ("skip", "#664400"),
                     ("exit", "#cc6666")]:
            self._log.tag_configure(t, foreground=c)

    # ── Settings ──

    def _open_settings(self):
        win = tk.Toplevel(self)
        win.title("Settings")
        win.geometry("580x650")
        win.configure(bg=BG)
        win.transient(self)

        config = self._read_config()
        settings = {}
        toggles = {}

        canvas = tk.Canvas(win, bg=BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(win, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=BG)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(fill="both", expand=True)

        def mk_s(t):
            tk.Label(inner, text=t, font=("Consolas", 10, "bold"), fg=ACCENT, bg=BG).pack(
                fill="x", padx=18, pady=(12, 4))
            tk.Frame(inner, bg=BORDER, height=1).pack(fill="x", padx=18)

        def mk_sl(label, key, val, lo, hi, suf, scale=1.0):
            f = tk.Frame(inner, bg=BG)
            f.pack(fill="x", padx=18, pady=3)
            tk.Label(f, text=label, font=("Consolas", 8), fg=TEXT, bg=BG,
                    width=26, anchor="w").pack(side="left")
            v = tk.DoubleVar(value=val)
            settings[key] = (v, scale)
            ttk.Scale(f, from_=lo, to=hi, variable=v, orient="horizontal",
                     length=180).pack(side="left", padx=5)
            vl = tk.Label(f, text=f"{val:.0f}{suf}", font=("Consolas", 9, "bold"),
                         fg=GREEN, bg=BG, width=8)
            vl.pack(side="left")
            v.trace_add("write", lambda *_, v=v, l=vl, s=suf: l.configure(
                text=f"{v.get():.0f}{s}"))

        def mk_toggle(label, key, val):
            toggles[key] = tk.BooleanVar(value=val)
            f = tk.Frame(inner, bg=BG)
            f.pack(fill="x", padx=18, pady=2)
            tk.Checkbutton(f, text=label, variable=toggles[key], font=("Consolas", 8),
                          fg=TEXT, bg=BG, selectcolor=BG2, activebackground=BG,
                          activeforeground=GREEN).pack(anchor="w")

        mk_s("Position Sizing")
        mk_sl("Balance per trade (%)", "SIZING_BALANCE_FRACTION",
               config.get("SIZING_BALANCE_FRACTION", 0.30) * 100, 5, 70, "%", 0.01)
        mk_sl("Max dollars per trade", "SIZING_MAX_DOLLARS",
               config.get("SIZING_MAX_DOLLARS", 50), 5, 200, "$")
        mk_sl("Daily loss limit ($)", "DAILY_LOSS_LIMIT",
               config.get("DAILY_LOSS_LIMIT", 15), 5, 100, "$")

        mk_s("Entry Rules")
        mk_sl("Min entry (cents)", "MIN_ENTRY_CENTS",
               config.get("MIN_ENTRY_CENTS", 40), 20, 50, "c")
        mk_sl("Max entry (cents)", "MAX_ENTRY_CENTS",
               config.get("MAX_ENTRY_CENTS", 55), 45, 70, "c")
        mk_sl("Min minutes remaining", "MIN_MINUTES_REMAINING",
               config.get("MIN_MINUTES_REMAINING", 2), 1, 12, " min")

        mk_s("Features")
        mk_toggle("Divergence ladder (spread bids when momentum opposes)",
                   "DIVERGENCE_LADDER_ENABLED",
                   config.get("DIVERGENCE_LADDER_ENABLED", 1) == 1)
        mk_toggle("Price action entry (react to contract moves + trade flow)",
                   "PRICE_ACTION_ENABLED",
                   config.get("PRICE_ACTION_ENABLED", 1) == 1)
        mk_toggle("TA fallback (legacy: 1m BTC candle TA)",
                   "TA_FORCED_ENABLED",
                   config.get("TA_FORCED_ENABLED", 0) == 1)
        mk_toggle("MTF shadow mode (log scores only, don't filter)",
                   "MTF_SHADOW_MODE",
                   config.get("MTF_SHADOW_MODE", 0) == 1)
        mk_toggle("Sell ladder (tiered sells at 3 price levels)",
                   "SELL_LADDER_ENABLED",
                   config.get("SELL_LADDER_ENABLED", 1) == 1)
        mk_toggle("Flip re-entry (small opposing trade after sell ladder fills)",
                   "FLIP_REENTRY_ENABLED",
                   config.get("FLIP_REENTRY_ENABLED", 1) == 1)

        mk_s("Trading Schedule")
        tk.Label(inner, text="Select hours to trade (ET). Uncheck to block.",
                 font=("Consolas", 7), fg=DIM, bg=BG).pack(anchor="w", padx=18)

        blocked = set()
        if CONFIG_PATH.exists():
            for line in CONFIG_PATH.read_text(encoding="utf-8").split("\n"):
                if line.strip().startswith("BLOCKED_HOURS"):
                    blocked = {int(n) for n in re.findall(r'\d+', line)}

        hour_vars = {}
        for name, hours in [("Night", range(0, 6)), ("Morning", range(6, 12)),
                             ("Afternoon", range(12, 18)), ("Evening", range(18, 24))]:
            pf = tk.Frame(inner, bg=BG)
            pf.pack(fill="x", padx=18, pady=1)
            tk.Label(pf, text=f"{name}:", font=("Consolas", 7, "bold"), fg=ORANGE,
                    bg=BG, width=10, anchor="w").pack(side="left")
            for h in hours:
                v = tk.BooleanVar(value=h not in blocked)
                hour_vars[h] = v
                tk.Checkbutton(pf, text=f"{h:02d}", variable=v, font=("Consolas", 7),
                              fg=TEXT, bg=BG, selectcolor=BG2,
                              activebackground=BG).pack(side="left", padx=1)

        preset_f = tk.Frame(inner, bg=BG)
        preset_f.pack(fill="x", padx=18, pady=4)
        for label, hrs in [("24/7", range(24)), ("US Hours", range(8, 20)), ("Off", [])]:
            def mk(h=hrs):
                for hh, v in hour_vars.items():
                    v.set(hh in h)
            tk.Button(preset_f, text=label, font=("Consolas", 7), fg=TEXT, bg=BG2,
                      relief="flat", padx=6, command=mk).pack(side="left", padx=2)

        mk_s("Account")
        tk.Button(inner, text="Change API Credentials", font=("Consolas", 8),
                  fg=TEXT, bg=BG2, relief="flat", padx=10, pady=4,
                  command=lambda: [win.destroy(), self._change_creds()]).pack(
                      padx=18, anchor="w", pady=6)

        def save():
            if not CONFIG_PATH.exists():
                return
            lines = CONFIG_PATH.read_text(encoding="utf-8").split("\n")
            new = []
            for line in lines:
                mod = False
                for k, (v, sc) in settings.items():
                    if re.match(rf'^{k}\s*=', line):
                        val = v.get() * sc
                        new.append(f"{k} = {int(val)}" if val == int(val) else f"{k} = {val:.2f}")
                        mod = True
                        break
                if not mod:
                    for k, v in toggles.items():
                        if line.strip().startswith(k):
                            new.append(f"{k} = {'True' if v.get() else 'False'}")
                            mod = True
                            break
                if not mod and line.strip().startswith("BLOCKED_HOURS"):
                    bl = {h for h, v in hour_vars.items() if not v.get()}
                    new.append(f"BLOCKED_HOURS = {{{','.join(str(h) for h in sorted(bl))}}}"
                              if bl else "BLOCKED_HOURS = set()")
                    mod = True
                if not mod:
                    new.append(line)
            CONFIG_PATH.write_text("\n".join(new), encoding="utf-8")
            self._stop_engine()
            win.destroy()
            messagebox.showinfo("Saved", "Settings saved. Restarting engine in 3 seconds...")
            self.after(3000, self._start_engine)

        tk.Button(inner, text="Save & Restart Engine", font=("Consolas", 11, "bold"),
                  fg="white", bg=ACCENT, relief="flat", padx=18, pady=8,
                  command=save).pack(pady=18)

    def _change_creds(self):
        self._stop_engine()
        self._first_launch()
        self._start_engine()

    def _read_config(self):
        c = {}
        if CONFIG_PATH.exists():
            for l in CONFIG_PATH.read_text(encoding="utf-8").split("\n"):
                m = re.match(r'^(\w+)\s*=\s*([0-9.]+)', l)
                if m:
                    try:
                        c[m.group(1)] = float(m.group(2))
                    except ValueError:
                        pass
        return c

    # ── Polling ──

    def _poll(self):
        self._phase += 0.2
        r = 4 + 2 * math.sin(self._phase)

        if self._nssm_mode:
            try:
                result = subprocess.run(["nssm", "status", "BTCBiasEngine"],
                    capture_output=True, text=True, timeout=2, creationflags=NO_WIN)
                running = "SERVICE_RUNNING" in result.stdout
            except Exception:
                running = False
        else:
            running = self._proc is not None and self._proc.poll() is None

        clr = GREEN if running else RED
        self._dot.delete("all")
        self._dot.create_oval(8 - r, 8 - r, 8 + r, 8 + r, fill=clr, outline="")
        mode = " (NSSM)" if self._nssm_mode else ""
        self._stat_lbl.configure(text=f"LIVE{mode}" if running else "STOPPED", fg=clr)

        if not self._reading:
            self._reading = True
            threading.Thread(target=self._bg_read, daemon=True).start()
        for _ in range(min(50, len(self._pending))):
            if self._pending:
                self._parse(self._pending.popleft())
        self._refresh()
        self.after(1000, self._poll)

    def _bg_read(self):
        try:
            if LOG_PATH.exists():
                sz = LOG_PATH.stat().st_size
                if sz != self._last_sz:
                    with open(LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
                        if self._last_sz > 0 and sz > self._last_sz:
                            f.seek(self._last_sz)
                            new = f.readlines()
                        else:
                            new = f.readlines()[-80:]
                        self._last_sz = sz
                        for l in new:
                            self._pending.append(l.strip())
        except Exception:
            pass
        self._reading = False

    def _refresh(self):
        self._bal_l.configure(text=f"${self._balance:.2f}")
        pc = GREEN if self._pnl >= 0 else RED
        self._pnl_l.configure(text=f"${self._pnl:+.2f} today", fg=pc)
        t = self._wins + self._losses
        wr = self._wins / t * 100 if t else 0
        self._wl_l.configure(
            text=f"{self._wins}W {self._losses}L ({wr:.0f}%)" if t else "",
            fg=GREEN if wr >= 55 else ORANGE if wr >= 40 else RED if t else DIM)
        self._skip_l.configure(
            text=f"{self._skips} filtered" if self._skips else "",
            fg=DIM)
        self._pos_l.configure(text=self._pos or "idle",
                              fg=PURPLE if self._pos else DIM)
        self._win_l.configure(text=self._window)

        if self._mid >= 57:
            self._dir_l.configure(text="YES", fg=GREEN)
        elif self._mid <= 43:
            self._dir_l.configure(text=" NO", fg=RED)
        elif 47 <= self._mid <= 53:
            self._dir_l.configure(text="DEAD", fg="#664400")
        else:
            self._dir_l.configure(text=" ~~", fg=ORANGE)

        cc = GREEN if self._conv >= 45 else ORANGE if self._conv >= 20 else DIM
        self._conv_l.configure(text=f"{self._conv} pts", fg=cc)
        self._conv_d.configure(text=self._conv_r)
        self._sizing_l.configure(
            text=f"sizing: {self._sizing_pct}%" if self._sizing_pct else "",
            fg=CYAN if self._sizing_pct >= 50 else DIM)

        b, l = self._bars["lean"]
        b.set(0.5 + (self._mid - 50) / 100,
              GREEN if self._mid > 57 else RED if self._mid < 43 else ORANGE if 47 <= self._mid <= 53 else DIM)
        l.configure(text=f"mid={self._mid}c" + (" DEAD ZONE" if 47 <= self._mid <= 53 else ""))

        b, l = self._bars["ta"]
        b.set(self._ta_conf / 100,
              GREEN if self._ta_conf >= 55 else ORANGE if self._ta_conf >= 30 else DIM)
        l.configure(text=f"conf={self._ta_conf} RSI={self._ta_rsi}"
                    + (" (gate: 55)" if self._ta_conf < 55 else ""))

        b, l = self._bars["mtf"]
        b.set(0.5 + self._mtf_score / 2,
              GREEN if self._mtf_score > 0.3 else RED if self._mtf_score < -0.3 else DIM)
        l.configure(text=f"{self._mtf_score:+.2f} {self._mtf_regime}")

        b, l = self._bars["flow"]
        b.set(self._flow_p, CYAN if self._flow_d else DIM)
        l.configure(text=f"{self._flow_d or 'neutral'} {self._flow_p:.0%}")

        b, l = self._bars["mom"]
        l.configure(text=self._mom or "--")

        for k, (badge, ac) in self._badges.items():
            on = ((k == "burst" and self._burst) or
                  (k == "ladder" and self._ladder) or
                  (k == "mtf" and bool(self._mtf_regime)) or
                  (k == "whale" and bool(self._whale)) or
                  (k == "flip" and "FLIP" in self._pos) or
                  (k == "skip" and self._skips > 0))
            badge.configure(bg=ac if on else DIM)

    def _log_add(self, text, tag="info"):
        self._log.configure(state="normal")
        ts = datetime.now().strftime("%H:%M:%S")
        self._log.insert("end", f" {ts}  {text}\n", tag)
        self._log.see("end")
        n = int(self._log.index("end-1c").split(".")[0])
        if n > 300:
            self._log.delete("1.0", f"{n - 300}.0")
        self._log.configure(state="disabled")

    # ── Log Parser ──

    def _parse(self, line):
        # Balance
        m = re.search(r'Balance synced.*\$([0-9.]+)', line)
        if m:
            self._balance = float(m.group(1))
        m = re.search(r'BALANCE CHECK: \$([0-9.]+)', line)
        if m:
            self._balance = float(m.group(1))

        # Daily P&L
        m = re.search(r'today total: \$([0-9.-]+)', line)
        if m:
            self._pnl = float(m.group(1))

        # Signal & mid
        m = re.search(r'SIGNAL: (YES|NO) \| mid=(\d+)c', line)
        if m:
            self._mid = int(m.group(2))
            self._mid_chart.add(self._mid)

        # TA
        m = re.search(r'TA warmed:.*conf=(\d+).*rsi=(\d+)', line)
        if m:
            self._ta_conf = int(m.group(1))
            self._ta_rsi = int(m.group(2))

        # MTF
        m = re.search(r'MTF.*score=([0-9.+-]+)\s+regime=(\w+)', line)
        if m:
            self._mtf_score = float(m.group(1))
            self._mtf_regime = m.group(2)

        # Conviction
        m = re.search(r'CONVICTION: (\d+) pts \[([^\]]*)\].*?(\d+)% sizing', line)
        if m:
            self._conv = int(m.group(1))
            self._conv_r = m.group(2)
            self._sizing_pct = int(m.group(3))
        elif re.search(r'CONVICTION: (\d+) pts \[([^\]]*)\]', line):
            m2 = re.search(r'CONVICTION: (\d+) pts \[([^\]]*)\]', line)
            self._conv = int(m2.group(1))
            self._conv_r = m2.group(2)

        # Flow
        m = re.search(r'FLOW: (\w+) \| pressure=([0-9.]+)%', line)
        if m:
            self._flow_d = m.group(1)
            self._flow_p = float(m.group(2)) / 100
            self._log_add(f"FLOW {m.group(1)} {m.group(2)}%", "flow")

        # Entries
        m = re.search(r'TA_FORCED ENTRY: (YES|NO).*?(\d+)x @ (\d+)c', line)
        if m:
            self._pos = f"{m.group(1)} {m.group(2)}x @ {m.group(3)}c"
            self._entry_px = int(m.group(3))
            self._mid_chart.set_entry(self._entry_px)
            self._log_add(f"ENTRY {m.group(1)} {m.group(2)}x @ {m.group(3)}c", "entry")

        # TP orders
        m = re.search(r'TIERED TP: \[([^\]]+)\]', line)
        if m:
            tps = re.findall(r'@(\d+)c', m.group(1))
            if tps:
                self._mid_chart.set_tps([int(t) for t in tps])

        # Burst / Ladder
        if "OPEN BURST" in line:
            self._burst = True
            self._log_add("BURST detected", "burst")
        if "DIVERGENCE LADDER" in line:
            self._ladder = True
            m = re.search(r'\[([^\]]+)\]', line)
            self._log_add(f"LADDER {m.group(1) if m else ''}", "ladder")

        # Sell ladder fills
        m = re.search(r'SELL LADDER: (\d+)x sold avg (\d+)c.*\+\$([0-9.]+).*?(\d+)x remaining', line)
        if m:
            sold, avg, pnl_str, remain = m.group(1), m.group(2), m.group(3), m.group(4)
            p = float(pnl_str)
            self._cum_pnl += p
            self._pnl_chart.add(self._cum_pnl)
            if int(remain) <= 0:
                self._wins += 1
                self._pos = ""
                self._mid_chart.set_entry(0)
                self._mid_chart.set_tps([])
            self._log_add(f"SELL {sold}x@{avg}c +${p:.2f} ({remain}x left)", "win")

        # Sell tier individual fills
        m = re.search(r'SELL TIER FILLED: (\d+)x @ (\d+)c \(\+(\d+)c\)', line)
        if m:
            self._log_add(f"TIER {m.group(1)}x@{m.group(2)}c (+{m.group(3)}c)", "win")

        # Sell ladder placed
        m = re.search(r'SELL LADDER: \[([^\]]+)\] entry=(\d+)c', line)
        if m:
            tps = re.findall(r'@(\d+)c', m.group(1))
            if tps:
                self._mid_chart.set_tps([int(t) for t in tps])

        # Flip re-entry
        m = re.search(r'FLIP RE-ENTRY: (YES|NO) (\d+)x @ (\d+)c.*prev (\w+) won \+\$([0-9.]+)', line)
        if m:
            self._pos = f"FLIP {m.group(1)} {m.group(2)}x @ {m.group(3)}c"
            self._entry_px = int(m.group(3))
            self._mid_chart.set_entry(self._entry_px)
            self._log_add(
                f"FLIP {m.group(1)} {m.group(2)}x@{m.group(3)}c (prev {m.group(4)} +${m.group(5)})",
                "entry")

        # Flip sell ladder
        m = re.search(r'FLIP SELL LADDER: \[([^\]]+)\]', line)
        if m:
            tps = re.findall(r'@(\d+)c', m.group(1))
            if tps:
                self._mid_chart.set_tps([int(t) for t in tps])

        # Extreme half-exit
        m = re.search(r'EXTREME HALF-EXIT: (\d+)x sold.*\$([0-9.+-]+).*holding (\d+)x', line)
        if m:
            p = float(m.group(2))
            self._cum_pnl += p
            self._pnl_chart.add(self._cum_pnl)
            self._log_add(f"EXTREME HALF {m.group(1)}x ${p:+.2f} (holding {m.group(3)}x)", "exit")

        # Priced-in exit
        m = re.search(r'PRICED-IN EXIT: (\d+)x sold @ (\d+)c.*\$\+?([0-9.]+)', line)
        if m:
            p = float(m.group(3))
            self._wins += 1
            self._pos = ""
            self._mid_chart.set_entry(0)
            self._mid_chart.set_tps([])
            self._cum_pnl += p
            self._pnl_chart.add(self._cum_pnl)
            self._log_add(f"PRICED-IN {m.group(1)}x@{m.group(2)}c +${p:.2f}", "win")

        # Profit lock
        m = re.search(r'PROFIT LOCK: (\d+)x sold @ (\d+)c.*\$\+?([0-9.]+).*?([0-9.]+) min', line)
        if m:
            p = float(m.group(3))
            self._wins += 1
            self._pos = ""
            self._mid_chart.set_entry(0)
            self._cum_pnl += p
            self._pnl_chart.add(self._cum_pnl)
            self._log_add(f"LOCK {m.group(1)}x@{m.group(2)}c +${p:.2f} ({m.group(4)}m left)", "win")

        # Price action signal
        m = re.search(r'PRICE_ACTION: (YES|NO) phase=(\d).*mid_move=([0-9+-]+)c.*votes=(\d)U/(\d)D.*budget=(\d+)%', line)
        if m:
            side, phase, move, vu, vd, bud = m.groups()
            self._log_add(f"PA {side} ph{phase} move={move}c votes={vu}U/{vd}D size={bud}%", "entry")

        # Confidence ladder placed
        m = re.search(r'CONFIDENCE LADDER: (YES|NO) \[([^\]]+)\].*conv=(\d+)', line)
        if m:
            self._log_add(f"BUY LADDER {m.group(1)} [{m.group(2)}] conv={m.group(3)}", "entry")

        # Legacy TP wins (pre-sell-ladder)
        m = re.search(r'TP SELLS DONE: \+\$([0-9.]+)', line)
        if m:
            p = float(m.group(1))
            self._wins += 1
            self._pos = ""
            self._mid_chart.set_entry(0)
            self._mid_chart.set_tps([])
            self._cum_pnl += p
            self._pnl_chart.add(self._cum_pnl)
            self._log_add(f"WIN +${p:.2f}", "win")

        # Losses / exits
        for pat, lbl, tag in [
            (r'HARD CLOSE:.*\$([0-9.+-]+)', "HARD CLOSE", "loss"),
            (r'HARD STOP:.*\$([0-9.+-]+)', "STOP", "loss"),
            (r'MANDATORY EXIT:.*\$([0-9.+-]+)', "MANDATORY", "loss"),
            (r'MTF REVERSAL EXIT:.*\$([0-9.+-]+)', "MTF REV", "mtf"),
            (r'NO-BOUNCE EXIT:.*\$([0-9.+-]+)', "NO-BOUNCE", "exit"),
            (r'EXTREME EXIT:.*\$([0-9.+-]+)', "EXTREME", "exit"),
            (r'EXPIRY:.*-\$([0-9.]+)', "EXPIRY", "loss"),
        ]:
            m = re.search(pat, line)
            if m:
                p = float(m.group(1))
                if p > 0 and "+" not in m.group(1):
                    p = -p  # Make losses negative
                self._losses += 1
                self._pos = ""
                self._mid_chart.set_entry(0)
                self._mid_chart.set_tps([])
                self._cum_pnl += p
                self._pnl_chart.add(self._cum_pnl)
                self._log_add(f"{lbl} ${p:+.2f}", tag)
                break

        # Filter skips
        if "ADX SKIP" in line:
            self._skips += 1
            self._log_add("ADX trending -- skipped", "skip")
        if "EXEC SKIP: entry outside range" in line:
            self._skips += 1
            m = re.search(r'ask=(\d+)c', line)
            self._log_add(f"SKIP: ask={m.group(1)}c outside range" if m else "SKIP: price out of range", "skip")

        # Whale
        m = re.search(r'WHALE.*: (\w+) (INFLOW|OUTFLOW) ([0-9.]+) BTC', line)
        if m:
            self._whale = f"{m.group(2)} {m.group(3)}"
            self._log_add(f"WHALE {m.group(1)} {m.group(2)} {m.group(3)} BTC", "whale")

        # Momentum
        m = re.search(r'OPEN MOMENTUM: (UP|DOWN) ([0-9+-]+)c.*\(([0-9.]+)c/s\)', line)
        if m:
            d, mv, rate = m.groups()
            self._mom = f"{d} {mv}c ({rate}c/s)"
            b, _ = self._bars["mom"]
            b.set(0.7 if d == "UP" else 0.3, GREEN if d == "UP" else RED)

        # Window
        m = re.search(r'new Poly window.*?(\d+:\d+[AP]M-\d+:\d+[AP]M ET)', line)
        if m:
            self._window = m.group(1)
            self._burst = False
            self._ladder = False
            self._flow_d = ""
            self._flow_p = 0.5
            self._whale = ""
            self._mom = ""
            self._sizing_pct = 0
            self._log_add(f"--- {m.group(1)} ---", "info")

        # Heartbeat mid-price
        m = re.search(r'snapshot.*mid=(\d+)c', line)
        if m:
            self._mid = int(m.group(1))
            self._mid_chart.add(self._mid)


if __name__ == "__main__":
    App().mainloop()
