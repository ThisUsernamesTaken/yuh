"""
BTC Bias Engine — Control Panel
the knobs, the dials, the money printer.
double click CONTROL_PANEL.pyw to open this.
"""
import os
import sys
import subprocess
import shutil
import json
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
from datetime import datetime
import re
import threading

# Auto-detect install directory — use exe dir in frozen mode, script dir otherwise
if getattr(sys, "frozen", False):
    INSTALL_DIR = Path(sys.executable).resolve().parent
else:
    INSTALL_DIR = Path(__file__).resolve().parent
CRED_DIR = INSTALL_DIR.parent / "credentials" if INSTALL_DIR.parent.name == "btc-bias-engine" else INSTALL_DIR / "credentials"
CONFIG_PATH = INSTALL_DIR / "user_config.py"
LOG_PATH = INSTALL_DIR / "data" / "engine_history.log"

BG = "#0d1117"
BG2 = "#161b22"
BORDER = "#30363d"
TEXT = "#e6edf3"
DIM = "#8b949e"
ACCENT = "#58a6ff"
RED = "#f85149"
GREEN = "#3fb950"
ORANGE = "#f0883e"
PURPLE = "#d2a8ff"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("BTC Bias Engine")
        self.geometry("680x720")
        self.configure(bg=BG)
        self.resizable(True, True)

        # Auto-create directories and config on first launch
        (INSTALL_DIR / "data").mkdir(parents=True, exist_ok=True)
        CRED_DIR.mkdir(parents=True, exist_ok=True)
        uc = INSTALL_DIR / "user_config.py"
        ex = INSTALL_DIR / "user_config.example.py"
        if not uc.exists() and ex.exists():
            shutil.copy2(ex, uc)
        elif not uc.exists():
            # Create default config matching active PC values
            uc.write_text(
                "# BTC Bias Engine — User Config\n"
                "ENGINE_DIR = r\"" + str(INSTALL_DIR) + "\"\n"
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
                "MTF_TIMEFRAMES = [\"1m\", \"5m\", \"15m\", \"1h\"]\n"
                "PRICE_FEED_SYMBOL = \"btcusdt\"\n"
                "PRICE_FEED_WS_TIMEOUT_S = 30.0\n"
                "PAPER_TRADING = False\n"
                "PAPER_STARTING_BALANCE = 100.0\n"
                "PAPER_SLIPPAGE_CENTS = 1\n"
                "MAX_TRADES_PER_WINDOW = 6\n"
                "TRADE_COOLDOWN_SECONDS = 10\n"
                "MIN_MINUTES_REMAINING = 2.0\n"
                "MAX_MINUTES_REMAINING = 15.0\n",
                encoding="utf-8",
            )

        # State
        self._installed = (INSTALL_DIR / "main.py").exists()
        self._running = False
        self._balance = 0.0
        self._daily_pnl = 0.0
        self._wins = 0
        self._losses = 0
        self._last_log_size = 0

        self._build_header()

        # Notebook for tabs
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=BG2, foreground=DIM,
                        padding=[12, 6], font=("Segoe UI", 10))
        style.map("TNotebook.Tab",
                  background=[("selected", BG)],
                  foreground=[("selected", ACCENT)])

        self._nb = ttk.Notebook(self)
        self._nb.pack(fill="both", expand=True, padx=15, pady=(5, 15))

        if self._installed:
            self._build_dashboard_tab()
            self._build_settings_tab()
            self._build_setup_tab()
            self._nb.select(0)
        else:
            self._build_setup_tab()
            self._build_settings_tab()
            self._nb.select(0)

        self._poll()

    # ── Header ──

    def _build_header(self):
        hdr = tk.Frame(self, bg=BG)
        hdr.pack(fill="x", padx=15, pady=(12, 0))

        tk.Label(hdr, text="BTC Bias Engine", font=("Segoe UI", 18, "bold"),
                 fg=ACCENT, bg=BG).pack(side="left")
        tk.Label(hdr, text="money printer", font=("Segoe UI", 9),
                 fg=DIM, bg=BG).pack(side="left", padx=(8, 0), pady=(5, 0))

        self._status_dot = tk.Label(hdr, text="  ", font=("Segoe UI", 14),
                                     fg=RED, bg=BG)
        self._status_dot.pack(side="right", padx=(0, 5))
        self._status_text = tk.Label(hdr, text="STOPPED", font=("Segoe UI", 10, "bold"),
                                      fg=RED, bg=BG)
        self._status_text.pack(side="right")

        # Quick controls
        ctrl = tk.Frame(hdr, bg=BG)
        ctrl.pack(side="right", padx=20)
        self._start_btn = tk.Button(ctrl, text="Start", font=("Segoe UI", 9, "bold"),
                                     fg=BG, bg=GREEN, relief="flat", padx=10, pady=2,
                                     command=self._start_engine)
        self._start_btn.pack(side="left", padx=2)
        self._stop_btn = tk.Button(ctrl, text="Stop", font=("Segoe UI", 9, "bold"),
                                    fg=BG, bg=RED, relief="flat", padx=10, pady=2,
                                    command=self._stop_engine)
        self._stop_btn.pack(side="left", padx=2)

    # ── Dashboard Tab ──

    def _build_dashboard_tab(self):
        tab = tk.Frame(self._nb, bg=BG)
        self._nb.add(tab, text=" Dashboard ")

        # Metrics row
        metrics = tk.Frame(tab, bg=BG2)
        metrics.pack(fill="x", padx=5, pady=5)
        self._dash_vars = {}
        for i, (label, key, default) in enumerate([
            ("Balance", "bal", "$0.00"),
            ("Daily P&L", "pnl", "$0.00"),
            ("Record", "wl", "0W / 0L"),
            ("Position", "pos", "NONE"),
        ]):
            f = tk.Frame(metrics, bg=BG2, padx=15, pady=8)
            f.grid(row=0, column=i, sticky="nsew")
            metrics.columnconfigure(i, weight=1)
            tk.Label(f, text=label, font=("Segoe UI", 8), fg=DIM, bg=BG2).pack()
            var = tk.StringVar(value=default)
            self._dash_vars[key] = var
            tk.Label(f, textvariable=var, font=("Consolas", 14, "bold"),
                     fg=TEXT, bg=BG2).pack()

        # Log
        log_frame = tk.Frame(tab, bg=BG)
        log_frame.pack(fill="both", expand=True, padx=5, pady=5)
        self._log_text = tk.Text(log_frame, bg=BG, fg=TEXT, font=("Consolas", 9),
                                  wrap="word", highlightthickness=0, borderwidth=0,
                                  state="disabled", insertbackground=TEXT)
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self._log_text.yview)
        self._log_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self._log_text.pack(fill="both", expand=True)

        for tag, color in [("win", GREEN), ("loss", RED), ("entry", ACCENT),
                           ("info", DIM), ("paper", PURPLE), ("mtf", ORANGE)]:
            self._log_text.tag_configure(tag, foreground=color)

    # ── Settings Tab ──

    def _build_settings_tab(self):
        tab = tk.Frame(self._nb, bg=BG)
        self._nb.add(tab, text=" Settings ")

        # Load current config
        config = self._read_config()

        canvas = tk.Canvas(tab, bg=BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(tab, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=BG)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(fill="both", expand=True)

        self._settings = {}

        # Sizing section
        self._add_section(inner, "Position Sizing")
        self._add_slider(inner, "Balance per trade (%)", "SIZING_BALANCE_FRACTION",
                         config.get("SIZING_BALANCE_FRACTION", 0.25) * 100,
                         5, 70, "%", scale=0.01,
                         help="How much of your balance to risk per trade. 25% = safe, 50% = aggressive.")
        self._add_slider(inner, "Max dollars per trade", "SIZING_MAX_DOLLARS",
                         config.get("SIZING_MAX_DOLLARS", 50), 5, 200, "$",
                         help="Hard cap on dollar amount per trade regardless of balance %.")

        # Entry section
        self._add_section(inner, "Entry Rules")
        self._add_slider(inner, "Min entry price (cents)", "MIN_ENTRY_CENTS",
                         config.get("MIN_ENTRY_CENTS", 40), 20, 50, "c",
                         help="Cheapest price to buy. Lower = more speculative. 40c = proven safe.")
        self._add_slider(inner, "Max entry price (cents)", "MAX_ENTRY_CENTS",
                         config.get("MAX_ENTRY_CENTS", 55), 45, 70, "c",
                         help="Most expensive entry. Higher = more windows but thinner margins.")
        self._add_slider(inner, "Min minutes remaining", "MIN_MINUTES_REMAINING",
                         config.get("MIN_MINUTES_REMAINING", 5), 2, 12, " min",
                         help="Don't enter if less than this many minutes left in the window.")

        # Safety section
        self._add_section(inner, "Safety")
        self._add_slider(inner, "Daily loss limit ($)", "DAILY_LOSS_LIMIT",
                         config.get("DAILY_LOSS_LIMIT", 15), 5, 100, "$",
                         help="Engine stops trading for the day after this much loss.")

        # Entry behavior
        self._add_section(inner, "Entry Behavior")
        ladder_f = tk.Frame(inner, bg=BG)
        ladder_f.pack(fill="x", padx=20, pady=5)

        ladder_enabled = config.get("DIVERGENCE_LADDER_ENABLED", 1) == 1
        self._ladder_var = tk.BooleanVar(value=ladder_enabled)
        tk.Checkbutton(ladder_f, text="Divergence ladder (spread bids when momentum opposes entry)",
                       variable=self._ladder_var, font=("Segoe UI", 9),
                       fg=TEXT, bg=BG, selectcolor=BG2, activebackground=BG,
                       activeforeground=GREEN).pack(anchor="w")
        tk.Label(ladder_f, text="When on: splits entry across 3 prices to catch dips. When off: single entry at best ask.",
                 font=("Segoe UI", 7), fg=DIM, bg=BG, wraplength=500).pack(anchor="w")

        # Startup behavior
        self._add_section(inner, "Engine Startup")
        startup_f = tk.Frame(inner, bg=BG)
        startup_f.pack(fill="x", padx=20, pady=5)
        self._auto_start_var = tk.BooleanVar(value=False)
        tk.Checkbutton(startup_f, text="Start engine automatically when Windows boots",
                       variable=self._auto_start_var, font=("Segoe UI", 9),
                       fg=TEXT, bg=BG, selectcolor=BG2, activebackground=BG,
                       activeforeground=GREEN).pack(anchor="w")
        tk.Label(startup_f, text="When off, you start the engine manually from this panel.",
                 font=("Segoe UI", 7), fg=DIM, bg=BG).pack(anchor="w")

        # Trading schedule section
        self._add_section(inner, "Trading Schedule")
        tk.Label(inner, text="Select hours to trade (Eastern Time). Uncheck to block.",
                 font=("Segoe UI", 8), fg=DIM, bg=BG).pack(anchor="w", padx=20, pady=(5, 3))

        # Parse current BLOCKED_HOURS from config
        blocked = set()
        raw_blocked = config.get("_BLOCKED_HOURS_RAW", "")
        # Re-read the raw line from config file
        if CONFIG_PATH.exists():
            for line in CONFIG_PATH.read_text(encoding="utf-8").split("\n"):
                if line.strip().startswith("BLOCKED_HOURS"):
                    nums = re.findall(r'\d+', line)
                    blocked = {int(n) for n in nums}

        self._hour_vars = {}
        schedule_frame = tk.Frame(inner, bg=BG)
        schedule_frame.pack(fill="x", padx=20, pady=5)

        # 4 rows x 6 columns of hour checkboxes
        periods = [
            ("Night", range(0, 6)),
            ("Morning", range(6, 12)),
            ("Afternoon", range(12, 18)),
            ("Evening", range(18, 24)),
        ]
        for row_idx, (period_name, hours) in enumerate(periods):
            period_frame = tk.Frame(schedule_frame, bg=BG)
            period_frame.pack(fill="x", pady=2)
            tk.Label(period_frame, text=f"{period_name}:", font=("Segoe UI", 8, "bold"),
                     fg=ORANGE, bg=BG, width=10, anchor="w").pack(side="left")
            for h in hours:
                var = tk.BooleanVar(value=(h not in blocked))
                self._hour_vars[h] = var
                label = f"{h:02d}"
                cb = tk.Checkbutton(period_frame, text=label, variable=var,
                                     font=("Consolas", 8), fg=TEXT, bg=BG,
                                     selectcolor=BG2, activebackground=BG,
                                     activeforeground=GREEN)
                cb.pack(side="left", padx=1)

        # Quick presets
        preset_frame = tk.Frame(inner, bg=BG)
        preset_frame.pack(fill="x", padx=20, pady=(3, 10))
        for label, hours_on in [
            ("24/7", range(24)),
            ("US Hours (8am-8pm)", range(8, 20)),
            ("US Market (9:30-4)", range(10, 16)),
            ("Overnight Only", list(range(0, 8)) + list(range(20, 24))),
            ("Off", []),
        ]:
            def make_preset(h_on=hours_on):
                for h, var in self._hour_vars.items():
                    var.set(h in h_on)
            tk.Button(preset_frame, text=label, font=("Segoe UI", 7),
                      fg=TEXT, bg=BG2, relief="flat", padx=6, pady=2,
                      command=make_preset).pack(side="left", padx=2)

        # Save button
        save_frame = tk.Frame(inner, bg=BG)
        save_frame.pack(fill="x", padx=20, pady=15)
        tk.Button(save_frame, text="Save & Restart Engine", font=("Segoe UI", 11, "bold"),
                  fg="white", bg=ACCENT, relief="flat", padx=20, pady=8,
                  command=self._save_settings).pack()

    def _add_section(self, parent, title):
        tk.Label(parent, text=title, font=("Segoe UI", 11, "bold"),
                 fg=ACCENT, bg=BG, anchor="w").pack(fill="x", padx=20, pady=(15, 5))
        tk.Frame(parent, bg=BORDER, height=1).pack(fill="x", padx=20)

    def _add_slider(self, parent, label, key, value, min_val, max_val, suffix, scale=1.0, help=""):
        f = tk.Frame(parent, bg=BG)
        f.pack(fill="x", padx=20, pady=4)

        tk.Label(f, text=label, font=("Segoe UI", 9), fg=TEXT, bg=BG,
                 width=25, anchor="w").pack(side="left")

        var = tk.DoubleVar(value=value)
        self._settings[key] = (var, scale)

        slider = ttk.Scale(f, from_=min_val, to=max_val, variable=var,
                           orient="horizontal", length=200)
        slider.pack(side="left", padx=(10, 5))

        val_label = tk.Label(f, text=f"{value:.0f}{suffix}", font=("Consolas", 10, "bold"),
                             fg=GREEN, bg=BG, width=8)
        val_label.pack(side="left")

        def update_label(*_):
            v = var.get()
            val_label.configure(text=f"{v:.0f}{suffix}")
        var.trace_add("write", update_label)

        if help:
            tk.Label(f, text=help, font=("Segoe UI", 7), fg=DIM, bg=BG,
                     wraplength=400, anchor="w").pack(side="left", padx=(10, 0))

    # ── Setup Tab ──

    def _build_setup_tab(self):
        tab = tk.Frame(self._nb, bg=BG)
        self._nb.add(tab, text=" Setup " if self._installed else " Install ")

        inner = tk.Frame(tab, bg=BG2, padx=30, pady=20)
        inner.pack(fill="x", padx=20, pady=20)

        tk.Label(inner, text="connect your kalshi account", font=("Segoe UI", 12, "bold"),
                 fg=ACCENT, bg=BG2).pack(anchor="w")
        tk.Label(inner, text="get these from kalshi.com/account/api", font=("Segoe UI", 8),
                 fg=DIM, bg=BG2).pack(anchor="w", pady=(0, 10))

        tk.Label(inner, text="API Key (UUID)", font=("Segoe UI", 9),
                 fg=DIM, bg=BG2, anchor="w").pack(fill="x")
        self._api_key_var = tk.StringVar()
        ttk.Entry(inner, textvariable=self._api_key_var, width=50,
                  font=("Consolas", 10)).pack(fill="x", pady=(2, 10))

        tk.Label(inner, text="RSA Private Key (.pem file)", font=("Segoe UI", 9),
                 fg=DIM, bg=BG2, anchor="w").pack(fill="x")
        pem_frame = tk.Frame(inner, bg=BG2)
        pem_frame.pack(fill="x", pady=(2, 10))
        self._pem_var = tk.StringVar()
        ttk.Entry(pem_frame, textvariable=self._pem_var, width=40,
                  font=("Consolas", 10)).pack(side="left", fill="x", expand=True)
        ttk.Button(pem_frame, text="Browse",
                   command=lambda: self._pem_var.set(
                       filedialog.askopenfilename(filetypes=[("PEM", "*.pem"), ("All", "*.*")])
                   )).pack(side="right", padx=(8, 0))

        tk.Label(inner, text="Mode", font=("Segoe UI", 9),
                 fg=DIM, bg=BG2, anchor="w").pack(fill="x")
        self._mode_var = tk.StringVar(value="live")
        mode_f = tk.Frame(inner, bg=BG2)
        mode_f.pack(fill="x", pady=(2, 10))
        ttk.Radiobutton(mode_f, text="Demo (paper)", variable=self._mode_var,
                        value="demo").pack(side="left", padx=(0, 15))
        ttk.Radiobutton(mode_f, text="Live (real money)", variable=self._mode_var,
                        value="live").pack(side="left")

        tk.Label(inner, text="Startup behavior", font=("Segoe UI", 9),
                 fg=DIM, bg=BG2, anchor="w").pack(fill="x")
        self._startup_var = tk.StringVar(value="manual")
        startup_f = tk.Frame(inner, bg=BG2)
        startup_f.pack(fill="x", pady=(2, 15))
        ttk.Radiobutton(startup_f, text="Manual (you click Start)", variable=self._startup_var,
                        value="manual").pack(side="left", padx=(0, 15))
        ttk.Radiobutton(startup_f, text="Auto (starts with Windows)", variable=self._startup_var,
                        value="auto").pack(side="left")

        tk.Button(inner, text="Save Credentials", font=("Segoe UI", 11, "bold"),
                  fg="white", bg=GREEN, relief="flat", padx=20, pady=8,
                  command=self._save_credentials).pack()

    # ── Actions ──

    _NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW flag — suppresses console flash

    def _start_engine(self):
        try:
            result = subprocess.run(["nssm", "status", "BTCBiasEngine"],
                capture_output=True, text=True, timeout=3,
                creationflags=self._NO_WINDOW)
            # NSSM is installed — use service management
            subprocess.run(["nssm", "start", "BTCBiasEngine"],
                capture_output=True, creationflags=self._NO_WINDOW)
        except (FileNotFoundError, Exception):
            # NSSM not installed — run engine directly
            try:
                if getattr(sys, "frozen", False):
                    # PyInstaller exe — relaunch ourselves with --engine flag
                    subprocess.Popen(
                        [sys.executable, "--engine"],
                        cwd=str(INSTALL_DIR),
                        creationflags=self._NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
                else:
                    # Running from source — launch main.py
                    subprocess.Popen(
                        [sys.executable, str(INSTALL_DIR / "main.py")],
                        cwd=str(INSTALL_DIR),
                        creationflags=self._NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
            except Exception as e:
                messagebox.showerror("Error", f"Could not start engine: {e}")

    def _stop_engine(self):
        try:
            subprocess.run(["nssm", "stop", "BTCBiasEngine"],
                capture_output=True, creationflags=self._NO_WINDOW)
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _save_settings(self):
        try:
            lines = CONFIG_PATH.read_text(encoding="utf-8").split("\n")
            new_lines = []
            for line in lines:
                modified = False
                # Slider settings
                for key, (var, scale) in self._settings.items():
                    pattern = rf'^{key}\s*='
                    if re.match(pattern, line):
                        val = var.get() * scale
                        if val == int(val):
                            new_lines.append(f"{key} = {int(val)}")
                        else:
                            new_lines.append(f"{key} = {val:.2f}")
                        modified = True
                        break
                # Divergence ladder toggle
                if not modified and line.strip().startswith("DIVERGENCE_LADDER_ENABLED"):
                    val = "True" if self._ladder_var.get() else "False"
                    new_lines.append(f"DIVERGENCE_LADDER_ENABLED = {val}")
                    modified = True
                # Blocked hours
                if not modified and line.strip().startswith("BLOCKED_HOURS"):
                    blocked = {h for h, var in self._hour_vars.items() if not var.get()}
                    if blocked:
                        new_lines.append(f"BLOCKED_HOURS = {{{','.join(str(h) for h in sorted(blocked))}}}")
                    else:
                        new_lines.append("BLOCKED_HOURS = set()")
                    modified = True
                if not modified:
                    new_lines.append(line)
            CONFIG_PATH.write_text("\n".join(new_lines), encoding="utf-8")

            # Update auto-start preference
            if hasattr(self, '_auto_start_var'):
                try:
                    start_type = "SERVICE_AUTO_START" if self._auto_start_var.get() else "SERVICE_DEMAND_START"
                    subprocess.run(["nssm", "set", "BTCBiasEngine", "Start", start_type],
                        capture_output=True, creationflags=0x08000000)
                except Exception:
                    pass

            # Stop, wait for current window to clear, then restart
            self._stop_engine()
            messagebox.showinfo("Saved",
                "Settings saved. Engine will restart in 5 seconds.\n"
                "The first window after restart is skipped for safety.")
            self.after(5000, self._start_engine)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save: {e}")

    def _save_credentials(self):
        api_key = self._api_key_var.get().strip()
        pem_path = self._pem_var.get().strip()
        if not api_key:
            messagebox.showerror("Missing", "Enter your API key.")
            return
        if not pem_path or not Path(pem_path).exists():
            messagebox.showerror("Missing", "Select a valid .pem file.")
            return

        try:
            CRED_DIR.mkdir(parents=True, exist_ok=True)
            pem_dest = CRED_DIR / "kalshi_key.pem"
            shutil.copy2(pem_path, pem_dest)

            is_demo = self._mode_var.get() == "demo"
            (CRED_DIR / "kalshi.env").write_text(
                f"KALSHI_API_KEY={api_key}\n"
                f"KALSHI_PRIVATE_KEY_PATH={str(pem_dest).replace(chr(92), '/')}\n"
                f"KALSHI_DEMO={'true' if is_demo else 'false'}\n"
                f"EXECUTE_TRADES={'false' if is_demo else 'true'}\n",
                encoding="utf-8",
            )
            # Set NSSM service start type
            auto_start = self._startup_var.get() == "auto"
            try:
                start_type = "SERVICE_AUTO_START" if auto_start else "SERVICE_DEMAND_START"
                subprocess.run(["nssm", "set", "BTCBiasEngine", "Start", start_type],
                    capture_output=True, creationflags=0x08000000)
            except Exception:
                pass

            start_msg = "Engine will start automatically with Windows." if auto_start else "Engine starts when you click Start in the Dashboard."
            # Ensure user_config.py exists
            uc = INSTALL_DIR / "user_config.py"
            ex = INSTALL_DIR / "user_config.example.py"
            if not uc.exists() and ex.exists():
                shutil.copy2(ex, uc)

            messagebox.showinfo("Saved", f"Credentials saved.\n\n{start_msg}")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _read_config(self):
        """Parse user_config.py into a dict of key=value."""
        config = {}
        if not CONFIG_PATH.exists():
            return config
        for line in CONFIG_PATH.read_text(encoding="utf-8").split("\n"):
            m = re.match(r'^(\w+)\s*=\s*([0-9.]+)', line)
            if m:
                try:
                    config[m.group(1)] = float(m.group(2))
                except ValueError:
                    pass
        return config

    # ── Polling ──

    def _poll(self):
        # Check service status (no console window flash)
        try:
            result = subprocess.run(["nssm", "status", "BTCBiasEngine"],
                                     capture_output=True, text=True, timeout=3,
                                     creationflags=0x08000000)
            running = "SERVICE_RUNNING" in result.stdout
            self._running = running
            if running:
                self._status_text.configure(text="RUNNING", fg=GREEN)
                self._status_dot.configure(fg=GREEN)
            else:
                self._status_text.configure(text="STOPPED", fg=RED)
                self._status_dot.configure(fg=RED)
        except Exception:
            pass

        # Parse log
        if hasattr(self, '_log_text') and LOG_PATH.exists():
            try:
                size = LOG_PATH.stat().st_size
                if size != self._last_log_size:
                    with open(LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
                        if self._last_log_size > 0 and size > self._last_log_size:
                            f.seek(self._last_log_size)
                            new = f.readlines()
                        else:
                            new = f.readlines()[-50:]
                        self._last_log_size = size
                        for line in new:
                            self._parse_log(line.strip())
            except Exception:
                pass

        self.after(2000, self._poll)

    def _parse_log(self, line):
        # Balance
        m = re.search(r'Balance synced.*\$([0-9.]+)', line)
        if m:
            self._balance = float(m.group(1))
        m = re.search(r'balance=\$([0-9.]+)', line)
        if m:
            self._balance = float(m.group(1))
        if hasattr(self, '_dash_vars'):
            self._dash_vars["bal"].set(f"${self._balance:.2f}")

        # Daily P&L
        m = re.search(r'today total: \$([0-9.-]+)', line)
        if m:
            self._daily_pnl = float(m.group(1))
            self._dash_vars["pnl"].set(f"${self._daily_pnl:+.2f}")

        # Entry
        m = re.search(r'TA_FORCED ENTRY: (YES|NO).*?(\d+)x @ (\d+)c', line)
        if m:
            self._dash_vars["pos"].set(f"{m.group(1)} {m.group(2)}x @ {m.group(3)}c")
            self._log_append(f"ENTRY {m.group(1)} {m.group(2)}x @ {m.group(3)}c", "entry")

        # Burst
        if "OPEN BURST" in line:
            self._log_append(line.split("CopyEngine ")[-1], "entry")

        # TP
        m = re.search(r'TP SELLS DONE: \+\$([0-9.]+)', line)
        if m:
            self._wins += 1
            self._dash_vars["wl"].set(f"{self._wins}W / {self._losses}L")
            self._dash_vars["pos"].set("NONE")
            self._log_append(f"WIN +${m.group(1)}", "win")

        # Stop/loss
        if "HARD STOP" in line or "MANDATORY EXIT" in line:
            self._losses += 1
            self._dash_vars["wl"].set(f"{self._wins}W / {self._losses}L")
            self._dash_vars["pos"].set("NONE")
            m = re.search(r'\$([0-9.-]+)', line)
            self._log_append(f"LOSS ${m.group(1) if m else '?'}", "loss")

        # MTF
        if "MTF REVERSAL" in line:
            self._log_append("MTF REVERSAL EXIT", "mtf")

        # Paper
        if "PAPER ENTRY" in line or "PAPER SETTLE" in line:
            short = line.split("CopyEngine ")[-1][:60]
            self._log_append(short, "paper")

        # Window
        if "new Poly window" in line:
            m = re.search(r'(\d+:\d+[AP]M-\d+:\d+[AP]M ET)', line)
            if m:
                self._log_append(f"--- {m.group(1)} ---", "info")

    def _log_append(self, text, tag="info"):
        if not hasattr(self, '_log_text'):
            return
        self._log_text.configure(state="normal")
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_text.insert("end", f"[{ts}] {text}\n", tag)
        self._log_text.see("end")
        lines = int(self._log_text.index("end-1c").split(".")[0])
        if lines > 300:
            self._log_text.delete("1.0", f"{lines - 300}.0")
        self._log_text.configure(state="disabled")


if __name__ == "__main__":
    app = App()
    app.mainloop()
