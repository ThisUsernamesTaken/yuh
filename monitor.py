"""BTC Bias Engine -- Terminal Monitor Dashboard v3.

Full-screen real-time display of all engine internals.
"""
import asyncio
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import threading
from datetime import datetime, timezone

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
WHITE = "\033[37m"
MAGENTA = "\033[35m"
BG_RED = "\033[41m"
BG_GREEN = "\033[42m"
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"
HOME = "\033[H"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE_DIR, "data", "dashboard_state.json")
LOG_PATH = os.path.join(BASE_DIR, "data", "engine_history.log")
DB_PATH = os.path.join(BASE_DIR, "data", "trades.db")
STALE_THRESHOLD = 10.0
B = "\u2588"  # full block
S = "\u2591"  # shade
H = "\u2593"  # medium shade
V = "\u2502"  # vertical line
HL = "\u2500" # horizontal line
TL = "\u250C"; TR = "\u2510"; BL = "\u2514"; BR = "\u2518"  # corners
TJ = "\u252C"; BJ = "\u2534"; LJ = "\u251C"; RJ = "\u2524"  # junctions


class KalshiBalanceFetcher:
    def __init__(self):
        self.balance_cents = 0
        self._running = False
    def start(self):
        self._running = True
        threading.Thread(target=self._loop, daemon=True).start()
    def stop(self):
        self._running = False
    def _loop(self):
        env_file = os.path.join(BASE_DIR, "credentials", "kalshi.env")
        if os.path.exists(env_file):
            with open(env_file) as f:
                for line in f:
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        k, v = line.split("=", 1)
                        os.environ[k.strip()] = v.strip()
        sys.path.insert(0, BASE_DIR)
        while self._running:
            try:
                from kalshi_client import KalshiClient
                kid = os.environ.get("KALSHI_API_KEY", "")
                pp = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
                pem = open(pp).read() if pp and os.path.exists(pp) else ""
                async def _f():
                    async with KalshiClient(kid, pem) as c:
                        self.balance_cents = (await c.get_balance()).balance
                asyncio.run(_f())
            except Exception:
                pass
            time.sleep(30)


class DashboardState:
    def __init__(self):
        self._data = {}
        self._last_mtime = 0.0
        self._history = []  # rolling score history for sparklines
    def refresh(self):
        try:
            st = os.stat(STATE_PATH)
            if st.st_mtime > self._last_mtime:
                with open(STATE_PATH, "r") as f:
                    self._data = json.load(f)
                self._last_mtime = st.st_mtime
                # Track pressure history for sparkline
                ps = self._data.get("pressure", {}).get("score", 0)
                self._history.append(ps)
                if len(self._history) > 60:
                    self._history = self._history[-60:]
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        return self._data
    @property
    def is_stale(self):
        return (time.time() - self._data.get("ts", 0)) > STALE_THRESHOLD
    @property
    def age(self):
        ts = self._data.get("ts", 0)
        return time.time() - ts if ts > 0 else 999
    @property
    def pressure_history(self):
        return self._history


class LogTailer:
    PATS = ["PRESSURE ENTRY","PRESSURE ARMED","BASELINE:","SIZING:","FILL:",
            "SELL TIER","SELL LADDER","SWEEP","BALANCE CHECK","REGIME:",
            "PROB TP","DYNAMIC TP","MANDATORY","HOLD","MARKET ENTRY",
            "REVERSAL","THESIS EXIT","TRAIL","P&L CORR","position closed","RE-ENTRY",
            "HOLD_EXPIRY TP","SCALP TP","SCALP DCA","SCALP STOP","PEAK GIVE",
            "PROB COLLAPSE","VWAP EXIT","CONVICTION BOOST","REVERSAL-RISK",
            "REGIME SKIP","VEL DEAD","LAG AGAINST","WINDOW LOCKED",
            # 2026-04-20 refinements (A,B,C,D)
            "PRESSURE CAP WIDEN","TRAIL HOLD","TRAIL RELEASE",
            "CONVICTION_DIP FIRED","CONVICTION_DIP_",
            "ESCROW LEAK","ESCROW SWEEP",
            # 2026-04-27 LATE_DOMINANT + GHOST safety
            "LATE_DOMINANT SIGNAL","LATE_DOMINANT MAKER","LATE_DOMINANT EXEC",
            "LATE_DOMINANT STOP","NO-TRADE-ZONE","GHOST IGNORED",
            "RESIDUAL-CLEAN","RESIDUAL FLATTEN","STUCK-RESIDUAL",
            "OVERSELL-DETECTED","REENTRY-BLOCK",
            # 2026-04-28 alpha set + safety hardening
            "ARB DETECTED","ARB COMPLETE","ARB PARTIAL","ARB EMERGENCY",
            "ARB REBALANCE","TAKER ESCALATE","TAKER FILL","TAKER MISS",
            "MAKER LATE FILL","MAKER UNFILLED","MICROPRICE-BID",
            "MANUAL FILL RECORDED","MANUAL FILL RAW","MANUAL TP PLACED",
            "MANUAL TP SKIP","MANUAL-DETECTED","BOUNDED CLOSE","PENDING:",
            "DAILY P&L","new Poly window","BALANCE SNAPSHOT","TP_FILLED"]
    def __init__(self):
        self._lines = []
    def tail(self, n=12):
        try:
            sz = os.path.getsize(LOG_PATH)
            with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
                f.seek(max(0, sz - 20000))
                raw = f.read()
            out = []
            for line in raw.splitlines():
                for p in self.PATS:
                    if p in line:
                        out.append(line)
                        break
            self._lines = out[-n:]
        except (FileNotFoundError, OSError):
            pass
        return self._lines


class TradeHistory:
    def __init__(self):
        self._trades = []
        self._last_q = 0.0
        self.stats = {}
        self.bal_history = []
    def refresh(self):
        if time.time() - self._last_q < 8.0:
            return self._trades
        try:
            conn = sqlite3.connect(DB_PATH, timeout=2)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("""SELECT placed_at, side, count, limit_price, status, pnl,
                         strategy_name, filled_count FROM kalshi_trades
                         WHERE status NOT IN ('pending','unfilled')
                         AND strategy_name LIKE '%TA_FORCED%'
                         ORDER BY id DESC LIMIT 12""")
            self._trades = [dict(r) for r in c.fetchall()]

            c.execute("""SELECT pnl FROM kalshi_trades
                         WHERE status NOT IN ('pending','unfilled')
                         AND strategy_name LIKE '%TA_FORCED%'
                         AND placed_at > datetime('now','-24 hours') ORDER BY id DESC""")
            rows = c.fetchall()
            wins = sum(1 for r in rows if (r["pnl"] or 0) > 0)
            losses = len(rows) - wins
            total = sum(r["pnl"] or 0 for r in rows)
            streak = 0
            st = ""
            for r in rows:
                p = r["pnl"] or 0
                if streak == 0:
                    st = "W" if p > 0 else "L"
                    streak = 1
                elif (p > 0 and st == "W") or (p <= 0 and st == "L"):
                    streak += 1
                else:
                    break
            self.stats = {"n": len(rows), "w": wins, "l": losses,
                          "wr": wins/len(rows)*100 if rows else 0,
                          "pnl": total, "streak": streak, "st": st}

            # Balance history for chart
            c.execute("""SELECT balance_cents FROM balance_snapshots
                         WHERE note='window_change' ORDER BY id DESC LIMIT 30""")
            self.bal_history = [r["balance_cents"]/100 for r in reversed(c.fetchall())]
            conn.close()
            self._last_q = time.time()
        except Exception:
            pass
        return self._trades


def _c(v, fmt="+.2f", p=GREEN, n=RED, z=DIM):
    if v > 0.005: return f"{p}{v:{fmt}}{RESET}"
    if v < -0.005: return f"{n}{v:{fmt}}{RESET}"
    return f"{z}{v:{fmt}}{RESET}"

def _sc(side):
    return f"{GREEN}{BOLD}YES{RESET}" if side.lower()=="yes" else f"{RED}{BOLD}NO{RESET}" if side.lower()=="no" else f"{DIM}--{RESET}"

def _spark(vals, w=20):
    if not vals: return DIM + HL*w + RESET
    blocks = " \u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"
    mn, mx = min(vals), max(vals)
    rng = mx - mn if mx != mn else 1
    out = ""
    for v in vals[-w:]:
        idx = min(8, int((v - mn) / rng * 8))
        c = GREEN if v > 0 else RED if v < 0 else DIM
        out += f"{c}{blocks[idx]}{RESET}"
    if len(vals) < w:
        out = DIM + HL*(w-len(vals)) + RESET + out
    return out

def _hbar(frac, w=16, c=CYAN):
    frac = max(0, min(1, frac))
    f = int(frac * w)
    return f"{c}{''.join(B for _ in range(f))}{RESET}{DIM}{''.join(S for _ in range(w-f))}{RESET}"

def _cbar(val, w=20, mx=1.0):
    half = w // 2
    bar = list(f"{DIM}{S}{RESET}" for _ in range(w))
    frac = max(-1, min(1, val / mx if mx else 0))
    ct = int(abs(frac) * half)
    clr = GREEN if frac > 0 else RED
    if frac > 0:
        for i in range(ct):
            if half+i < w: bar[half+i] = f"{clr}{B}{RESET}"
    else:
        for i in range(ct):
            if half-1-i >= 0: bar[half-1-i] = f"{clr}{B}{RESET}"
    bar[half] = f"{WHITE}{V}{RESET}"
    return "".join(bar)

def _ft(s):
    if s <= 0: return "0s"
    m, sec = divmod(int(s), 60)
    return f"{m}:{sec:02d}" if m else f"{sec}s"

def _pad(l, w):
    clean = re.sub(r'\033\[[^m]*m', '', l)
    return l + " " * max(0, w - len(clean))

def _box_top(w):
    return f"{DIM}{TL}{HL*(w-2)}{TR}{RESET}"
def _box_bot(w):
    return f"{DIM}{BL}{HL*(w-2)}{BR}{RESET}"
def _box_mid(w):
    return f"{DIM}{LJ}{HL*(w-2)}{RJ}{RESET}"
def _box_line(content, w):
    clean = re.sub(r'\033\[[^m]*m', '', content)
    maxc = w - 3
    if len(clean) > maxc:
        # Truncate preserving ANSI
        vis = 0
        i = 0
        while i < len(content) and vis < maxc:
            if content[i] == '\033':
                end = content.find('m', i)
                if end >= 0:
                    i = end + 1
                    continue
            vis += 1
            i += 1
        content = content[:i] + RESET
        clean = re.sub(r'\033\[[^m]*m', '', content)
    pad = max(0, maxc - len(clean))
    return f"{DIM}{V}{RESET} {content}{' '*pad}{DIM}{V}{RESET}"


# ═══════════════════════════════════════════════════════════════════════════
# NARRATIVE VIEW — human-readable story of what the engine is thinking
# ═══════════════════════════════════════════════════════════════════════════

def _wrap(text, width, indent=""):
    """Simple word-wrap preserving indent."""
    out = []
    words = text.split()
    cur = indent
    for w in words:
        if len(cur) + len(w) + 1 > width:
            out.append(cur.rstrip())
            cur = indent + w
        else:
            cur = (cur + " " + w) if cur.strip() else (indent + w)
    if cur.strip():
        out.append(cur.rstrip())
    return out


def _mark(ok):
    """Green ✓ / red ✗ based on bool."""
    return f"{GREEN}\u2713{RESET}" if ok else f"{RED}\u2717{RESET}"


def _narr_gate(data):
    """Lines describing the Dominant-Direction gate — the AND-filter that
    blocks entries unless BTC 5m trend, pressure direction+conviction, RSI
    zone, and FVG magnitude all agree with the proposed side.
    """
    g = data.get("dominant_gate", {}) or {}
    lines = []
    lines.append(f"{BOLD}DOMINANT-DIRECTION GATE{RESET}")
    if not g.get("evaluated"):
        lines.append(f"{DIM}Not yet evaluated this cycle — waiting for FVG divergence to propose a side.{RESET}")
        return lines

    side = (g.get("side", "") or "").upper() or "—"
    sc = GREEN if side == "YES" else RED if side == "NO" else DIM
    q = g.get("qualifies", False)
    verdict = f"{BG_GREEN}{BOLD} PASS {RESET}" if q else f"{BG_RED}{BOLD} BLOCK {RESET}"
    lines.append(f"Proposed {sc}{BOLD}{side}{RESET} entry — verdict {verdict}")
    lines.append(f"{DIM}All four factors must agree; any single ✗ blocks the entry.{RESET}")
    lines.append("")

    # Factor 1: BTC 5m trend
    btc5 = g.get("btc_5m_move", 0.0)
    ok1 = g.get("btc_dominant", False)
    want = "≥ +$30" if side == "YES" else "≤ -$30" if side == "NO" else "±$30"
    lines.append(f"  {_mark(ok1)} {BOLD}BTC 5m trend{RESET}  ${btc5:+.0f}  {DIM}(needs {want}){RESET}")

    # Factor 2: pressure aligned + confident
    pdir = (g.get("pressure_direction", "") or "—").upper()
    pconf = g.get("pressure_confidence", 0.0)
    ok2 = g.get("pressure_agrees_strong", False)
    pc_color = GREEN if pdir == side and pdir != "—" else RED if pdir and pdir != side else DIM
    lines.append(f"  {_mark(ok2)} {BOLD}Pressure aligned{RESET}  {pc_color}{pdir}{RESET} conf={pconf:.0%}  {DIM}(needs same side, conf ≥ 55%){RESET}")

    # Factor 3: RSI not contrarian
    rsi = g.get("rsi", 50.0)
    ok3 = g.get("rsi_not_contrarian", False)
    rwant = "≤ 70" if side == "YES" else "≥ 30" if side == "NO" else "30–70"
    rcol = GREEN if ok3 else RED
    lines.append(f"  {_mark(ok3)} {BOLD}RSI not contrarian{RESET}  {rcol}{rsi:.0f}{RESET}  {DIM}(needs {rwant}){RESET}")

    # Factor 4: FVG non-marginal
    fvg_abs = g.get("fvg_magnitude", 0)
    fvg_s = g.get("fvg_signed", 0)
    ok4 = g.get("fvg_non_marginal", False)
    lines.append(f"  {_mark(ok4)} {BOLD}FVG non-marginal{RESET}  {fvg_s:+d}c (|{fvg_abs}|)  {DIM}(needs ≥ 10c){RESET}")

    lines.append("")
    if q:
        lines.append(f"{GREEN}All four gates agree — April-15 profile matched. Sizing is live.{RESET}")
    else:
        # List which factor(s) blocked
        fails = []
        if not ok1: fails.append("BTC 5m")
        if not ok2: fails.append("pressure")
        if not ok3: fails.append("RSI")
        if not ok4: fails.append("FVG")
        lines.append(f"{YELLOW}Blocked by: {', '.join(fails)}. Waiting for alignment or a new proposed side.{RESET}")
    return lines


def _narr_entry_factors(pos):
    """Lines describing the factor snapshot at the moment the engine entered
    the active position. Lets the operator see exactly what signal package
    justified the trade.
    """
    ef = pos.get("entry_factors", {}) or {}
    eg = pos.get("entry_gate", {}) or {}
    if not ef:
        return []

    lines = []
    lines.append(f"{BOLD}WHAT DROVE THIS ENTRY{RESET}")

    ps = ef.get("pressure_score", 0.0)
    pc = ef.get("pressure_conf", 0.0)
    psc = GREEN if ps > 0 else RED if ps < 0 else DIM
    lines.append(f"Composite pressure at entry: {psc}{BOLD}{ps:+.2f}{RESET} at {pc:.0%} confidence.")

    comps = []
    for nm, key in [("BTC impulse","btc_impulse"), ("book", "book_pressure"),
                    ("flow", "flow_momentum"), ("lag", "kalshi_lag")]:
        v = ef.get(key, 0.0)
        c = GREEN if v > 0 else RED if v < 0 else DIM
        comps.append(f"{DIM}{nm}{RESET}={c}{v:+.2f}{RESET}")
    lines.append("Components: " + "  ".join(comps))

    btc5 = ef.get("btc_5m_move", 0.0)
    rsi = ef.get("rsi", 50.0)
    fvg = ef.get("fvg_signed", 0)
    persistence = ef.get("persistence", 0)
    b5c = GREEN if btc5 > 0 else RED if btc5 < 0 else DIM
    fvc = GREEN if fvg > 0 else RED if fvg < 0 else DIM
    lines.append(f"BTC 5m trend {b5c}${btc5:+.0f}{RESET}  RSI {rsi:.0f}  FVG {fvc}{fvg:+d}c{RESET}  persistence {persistence}/3")

    spread = ef.get("spread_cents", 0)
    sess_age = int(ef.get("session_age_s", 0))
    mins = sess_age // 60
    secs = sess_age % 60
    lines.append(f"Book spread {spread}c, session age {mins}:{secs:02d} when filled.")

    if eg.get("qualifies"):
        lines.append(f"{GREEN}All four gates passed at entry — this is a dominant-direction trade.{RESET}")
    elif eg:
        lines.append(f"{YELLOW}Entered via non-dominant path (pre-open arb / primary / fallback tier).{RESET}")
    return lines


def _narr_upgrades(data):
    """Lines describing missed-dominant opportunities and recent upgrade
    events. Shows the operator the cost of holding a scalp through
    opportunities that would have upgraded size or exit-mode had the
    engine been flat (or eligible).
    """
    md = data.get("missed_dominant", {}) or {}
    ru = data.get("recent_upgrades", []) or []
    ms = int(md.get("session", 0))
    mt = int(md.get("total", 0))
    upg_done = bool(md.get("upgrade_fired_this_window", False))

    if not (ms or mt or ru or upg_done):
        return []

    lines = []
    lines.append(f"{BOLD}MID-TRADE UPGRADES{RESET}")

    if upg_done:
        lines.append(f"{GREEN}\u2713 Size upgrade already fired this window{RESET} — one shot used, no more upsizes until next window.")
    elif ms == 0:
        lines.append(f"{DIM}No dominant-direction signals fired mid-trade this window.{RESET}")

    if ms > 0:
        sc = YELLOW if ms < 3 else RED
        lines.append(f"{sc}Dominant gate qualified {ms}x this window but we couldn't act{RESET} — already holding ineligible position (upgraded, HOLD_EXPIRY, or scalp-stopped).")
    if mt > 0:
        lines.append(f"{DIM}Lifetime missed opportunities (all windows): {mt}.{RESET}")

    if ru:
        lines.append("")
        lines.append(f"{DIM}Recent upgrade events (newest last):{RESET}")
        import time as _tm
        for evt in ru:
            kind = evt.get("kind", "")
            side = (evt.get("side", "") or "").upper()
            sc = GREEN if side == "YES" else RED if side == "NO" else DIM
            age_s = int(max(0, _tm.time() - evt.get("ts", 0)))
            age_tag = f"{age_s}s ago" if age_s < 60 else f"{age_s // 60}m ago"
            if kind == "size_upgrade":
                lines.append(
                    f"  \u2022 {sc}{side}{RESET} +{evt.get('added_ct',0)}ct @ {evt.get('price',0)}c "
                    f"{DIM}(new {evt.get('new_count',0)}ct avg {evt.get('new_avg_entry',0)}c, "
                    f"btc5m ${evt.get('btc_5m',0):+.0f}) {age_tag}{RESET}"
                )
            elif kind == "exit_upgrade":
                reason = evt.get("reason", "")
                lines.append(
                    f"  \u2022 {sc}{side}{RESET} SCALP\u2192HOLD_EXPIRY on {evt.get('count',0)}ct "
                    f"{DIM}(RSI={evt.get('rsi',50):.0f} prob={evt.get('prob',50):.0f}% "
                    f"reason={reason}) {age_tag}{RESET}"
                )
            else:
                lines.append(f"  \u2022 {sc}{side}{RESET} {kind} {DIM}{age_tag}{RESET}")
    return lines


def _narr_session(data, stale, age):
    """Return list of narrative lines explaining the session."""
    sess = data.get("session", {})
    prob = data.get("prob", {})
    btc_d = data.get("btc", {})
    fvg_d = data.get("fvg", {})
    tape = data.get("tape", {})
    prs = data.get("pressure", {})
    five_s = data.get("five_sec", {})

    lines = []
    secs = sess.get("seconds_remaining", 0)
    regime = sess.get("regime", "unknown")
    bl_set = sess.get("baseline_set", False)
    bl_price = sess.get("baseline_price", 0)
    bl_smp = sess.get("baseline_samples", 0)
    btc = btc_d.get("price", 0)
    strike = prob.get("strike", 0)
    pp = prob.get("probability", 0) * 100
    pside = prob.get("side", "")
    fv = prob.get("fair_value", 0)
    edge = prob.get("edge_cents", 0)
    vol = prob.get("volatility", 0)
    misp = prob.get("mispricing", 0)
    fvg = fvg_d.get("fvg_vs_baseline", 0)
    fvg_t = fvg_d.get("threshold", 5)
    mid = tape.get("mid_cents", 0)
    ps = prs.get("score", 0)
    pd = prs.get("direction", "")
    pc = prs.get("confidence", 0)
    pt_ = prs.get("threshold", 0.25)
    pp_ct = prs.get("persist_count", 0)
    pr = prs.get("ready", False)

    # ── Header line ──
    if stale:
        lines.append(f"{BG_RED}{BOLD} ENGINE OFFLINE ({int(age)}s) {RESET}")
        lines.append(f"{DIM}No state updates. Engine may have crashed. Check nssm status BTCBiasEngine.{RESET}")
        return lines

    mins = int(secs) // 60
    sec = int(secs) % 60
    phase = (
        "just opened" if secs > 780 else
        "still early" if secs > 600 else
        "mid-session" if secs > 300 else
        "late innings" if secs > 120 else
        "final sprint"
    )

    regime_desc = {
        "TRENDING": f"a {GREEN}trending{RESET} session — BTC is moving in one direction with conviction",
        "MEAN_REVERTING": f"a {CYAN}choppy{RESET} mean-reverting session — BTC is oscillating around a center",
        "EXPLOSIVE": f"an {RED}explosive{RESET} session — BTC is moving violently with wide swings",
    }.get(regime, f"a {DIM}warming-up{RESET} session")

    lines.append(f"{BOLD}SESSION READ{RESET}")
    lines.append(f"This is {regime_desc}.")
    lines.append(f"We're {phase} with {BOLD}{mins}:{sec:02d}{RESET} remaining on the clock.")
    lines.append("")

    # ── BTC + strike ──
    if btc > 0 and strike > 0:
        dist_pct = (btc - strike) / strike * 100
        dist_verb = "above" if dist_pct > 0 else "below"
        favors = "YES (BTC up)" if dist_pct > 0 else "NO (BTC down)"
        fc = GREEN if dist_pct > 0 else RED
        lines.append(f"BTC is at {BOLD}${btc:,.2f}{RESET}, {fc}{abs(dist_pct):.3f}%{RESET} {dist_verb} the ${strike:,.0f} strike.")
        lines.append(f"That favors {fc}{BOLD}{favors}{RESET} if it settles here.")

    # ── Probability ──
    if prob.get("is_ready"):
        # vol is stored as percent (e.g., 24.2 for 24.2%), not a fraction
        vol_label = "low" if vol < 20 else "normal" if vol < 35 else "elevated" if vol < 50 else "extreme"
        lines.append(f"Probability model says {BOLD}{pp:.0f}%{RESET} {_sc(pside)} — volatility is {vol_label} ({vol:.0f}%).")
        if abs(edge) >= 10:
            lines.append(f"The fair value is {BOLD}{fv:.0f}c{RESET}, a {YELLOW}{abs(edge):.0f}c mispricing{RESET} vs. the book.")
        elif abs(edge) >= 3:
            lines.append(f"Fair value is {fv:.0f}c — about {abs(edge):.0f}c of edge, weak but present.")
        else:
            lines.append(f"The book is priced close to fair value — no mispricing to exploit yet.")
    else:
        lines.append(f"{DIM}Probability engine still warming up — need more price data to model.{RESET}")
    lines.append("")

    # ── Baseline / FVG ──
    lines.append(f"{BOLD}BASELINE & DIVERGENCE{RESET}")
    if not bl_set:
        remain_smp = max(0, 90 - bl_smp)
        lines.append(f"Still collecting the baseline — {bl_smp}/90 samples, {remain_smp} more needed.")
        lines.append(f"{DIM}No entries allowed until baseline locks.{RESET}")
    else:
        lines.append(f"The session baseline locked at {BOLD}{bl_price}c{RESET} (the opening consensus mid).")
        if abs(fvg) >= fvg_t:
            side_verb = "YES" if fvg > 0 else "NO"
            sc = GREEN if fvg > 0 else RED
            lines.append(f"Fair value has drifted {sc}{abs(fvg):.0f}c above/below{RESET} that baseline ({YELLOW}FVG triggered{RESET}) — the market hasn't repriced yet, so {sc}{BOLD}{side_verb}{RESET} is underpriced.")
        else:
            pct = int(abs(fvg) / max(fvg_t, 1) * 100)
            lines.append(f"Fair value is only {abs(fvg):.0f}c from baseline ({pct}% of the {fvg_t}c trigger) — no divergence yet.")
    lines.append("")

    # ── Pressure ──
    lines.append(f"{BOLD}MICROSTRUCTURE PRESSURE{RESET}")
    if ps == 0 and pc == 0:
        lines.append(f"{DIM}Pressure engine building — no score yet.{RESET}")
    else:
        dir_word = "YES" if ps > 0 else "NO" if ps < 0 else "neutral"
        dc = GREEN if ps > 0 else RED if ps < 0 else DIM
        strength = (
            "very strong" if abs(ps) >= 0.50 else
            "strong" if abs(ps) >= 0.30 else
            "moderate" if abs(ps) >= 0.15 else
            "weak"
        )
        lines.append(f"Pressure score is {dc}{BOLD}{ps:+.2f}{RESET} ({strength}) pointing {dc}{dir_word}{RESET}.")
        lines.append(f"Component agreement: {pc*100:.0f}% — {_conf_desc(pc)}.")
        if pr:
            lines.append(f"{GREEN}{BOLD}Entry is armed.{RESET} Persistence {pp_ct}/3 passed and threshold {pt_:.2f} crossed.")
        elif prs.get("crossed"):
            lines.append(f"{YELLOW}Threshold crossed{RESET} — needs {3-pp_ct} more cycle(s) of persistence to arm.")
        else:
            pct = int(abs(ps) / max(pt_, 0.01) * 100)
            lines.append(f"Score is at {pct}% of the {pt_:.2f} trigger — still building.")
    lines.append("")

    # ── Dominant-Direction gate (live per-factor readout) ──
    lines.extend(_narr_gate(data))
    lines.append("")

    return lines


def _conf_desc(c):
    if c >= 0.95: return "all four components agree"
    if c >= 0.75: return "most components agree"
    if c >= 0.50: return "components split"
    return "components conflicting"


def _narr_position(data):
    """Return list of narrative lines explaining the current position or entry stance."""
    sess = data.get("session", {})
    prob = data.get("prob", {})
    fvg_d = data.get("fvg", {})
    tape = data.get("tape", {})
    prs = data.get("pressure", {})
    pos = data.get("position", {})

    lines = []
    secs = sess.get("seconds_remaining", 0)
    pp = prob.get("probability", 0) * 100
    fvg = fvg_d.get("fvg_vs_baseline", 0)
    mid = tape.get("mid_cents", 0)
    ps = prs.get("score", 0)
    pd = prs.get("direction", "")
    pc = prs.get("confidence", 0)
    pp_ct = prs.get("persist_count", 0)

    if pos.get("active"):
        side = pos.get("side", "")
        ct = pos.get("count", 0)
        entry = pos.get("entry_cents", 0)
        tp = pos.get("tp_price", 0)
        hwm = pos.get("high_water", 0)
        mae = pos.get("low_water", 0)
        unreal = pos.get("unrealized_cents", 0) / 100
        rev = pos.get("reversal_pct", 0)
        ep = pos.get("entry_prob", 50)
        cur_val = int(mid) if side == "yes" else 100 - int(mid)
        pnl_per = cur_val - entry
        dollars_at_risk = entry * ct / 100

        _em = pos.get("exit_mode", "SCALP")
        _ep_path = pos.get("exec_path", "")
        _dca_f = pos.get("scalp_dca_fired", False)
        _orig_ct = pos.get("original_count", ct)

        lines.append(f"{BOLD}POSITION NARRATIVE{RESET}")
        if _em == "HOLD_EXPIRY":
            lines.append(f"I'm holding {_sc(side)} {BOLD}{ct}x{RESET} at {entry}c — {GREEN}{BOLD}HOLD TO EXPIRY{RESET} mode.")
            lines.append(f"High conviction entry (RSI extreme or prob+vel contrarian).")
            lines.append(f"Tiered exits rest at 78c/85c/95c with a small slice riding to 100c settlement.")
            lines.append(f"{DIM}No stop loss active — trusting the directional math.{RESET}")
        else:
            lines.append(f"I'm holding {_sc(side)} {BOLD}{ct}x{RESET} at {entry}c — {CYAN}SCALP{RESET} mode.")
            lines.append(f"Target is {GREEN}{tp}c{RESET} (+{tp-entry}c from entry). Stop at {RED}{entry-10}c{RESET}.")
            if _dca_f:
                lines.append(f"{YELLOW}DCA fired{RESET} — averaged down from {_orig_ct}x. TP tightened to avg+3c.")
        if "CONVICTION_DIP" in _ep_path:
            lines.append(f"{MAGENTA}\u25C6 Conviction dip{RESET} — entered dip with baseline 60/30 sizing: {DIM}{_ep_path}{RESET}")
        elif "BOOST" in _ep_path:
            lines.append(f"{MAGENTA}Boosted sizing{RESET}: {_ep_path}")
        _held_nn = int(pos.get("_trail_held_count", 0) or 0)
        if _held_nn > 0:
            lines.append(f"{MAGENTA}Trail held {_held_nn} cycles{RESET} — pressure still on our side + tight book, deferring give-back exit.")
        lines.append(f"Currently worth {BOLD}{cur_val}c{RESET} — ${dollars_at_risk:.2f} at risk.")
        lines.append("")

        # P&L story
        unrealc = GREEN if unreal > 0 else RED if unreal < 0 else DIM
        progress = pnl_per / max(tp - entry, 1) * 100 if tp > entry else 0
        if pnl_per > 0:
            lines.append(f"{GREEN}We're {pnl_per:+d}c into profit{RESET} — unrealized {unrealc}{_c(unreal)}{RESET} ({progress:.0f}% to TP).")
        elif pnl_per > -3:
            lines.append(f"{DIM}Sitting {pnl_per:+d}c from entry — in noise range.{RESET}")
        else:
            lines.append(f"{RED}Underwater {pnl_per:+d}c{RESET} — unrealized {unrealc}{_c(unreal)}{RESET}.")

        # HWM / MAE story
        if hwm > entry + 2:
            give = hwm - cur_val
            if give >= 5 and hwm - entry >= 10:
                lines.append(f"Hit {CYAN}{hwm}c high{RESET} — giving back {YELLOW}{give}c{RESET}. Trailing stop may trigger.")
            elif hwm - entry >= 10:
                lines.append(f"Ran to {CYAN}{hwm}c{RESET} — trailing stop is {GREEN}armed{RESET}, needs {8-give}c more give-back to fire.")
            else:
                lines.append(f"Best print: {CYAN}{hwm}c{RESET}. Trailing stop arms at +10c from entry.")
        if mae < entry and entry - mae >= 3:
            lines.append(f"Worst print: {RED}{mae}c{RESET} — was {entry-mae}c underwater before recovering.")

        # Prob drift
        if ep > 0 and abs(ep - pp) >= 5:
            drift = pp - ep
            dc = GREEN if (drift > 0 and side == "yes") or (drift < 0 and side == "no") else RED
            lines.append(f"Probability drifted from {ep:.0f}% at entry to {pp:.0f}% now — {dc}{'+' if drift > 0 else ''}{drift:.0f}%{RESET}.")
        lines.append("")

        # ── Per-trade entry factors (what drove this fill) ──
        ef_lines = _narr_entry_factors(pos)
        if ef_lines:
            lines.extend(ef_lines)
            lines.append("")

        # ── Mid-trade upgrades + missed opportunities ──
        up_lines = _narr_upgrades(data)
        if up_lines:
            lines.extend(up_lines)
            lines.append("")

        # ── Engine decision narrative ──
        lines.append(f"{BOLD}ENGINE DECISION{RESET}")
        thesis_ok = not ((fvg < 0 and side == "yes") or (fvg > 0 and side == "no"))
        mand_zone = secs < 180

        if mand_zone:
            prob_favors = (pp > 55 and side == "yes") or (pp < 45 and side == "no")
            if prob_favors:
                lines.append(f"{MAGENTA}<3 minutes left but probability favors our side{RESET} — holding to settlement.")
            else:
                lines.append(f"{RED}<3 minutes left and we're off-side{RESET} — primed to exit if no recovery.")
        elif rev >= 60 and cur_val > entry:
            lines.append(f"{YELLOW}Soft reversal triggered{RESET} — {rev}% of the edge gave back. Locking profit at bid.")
        elif not thesis_ok:
            lines.append(f"{RED}Thesis broken{RESET} — FVG has flipped against us. Watching for exit signal.")
        elif pnl_per > 0:
            lines.append(f"{GREEN}Profitable and quiet{RESET} — waiting for TP limit at {tp}c to fill. No action needed.")
        else:
            lines.append(f"{DIM}Near entry — holding to the thesis. Stops are disabled; we ride this out.{RESET}")
        lines.append("")

        # ── What to watch ──
        lines.append(f"{BOLD}WHAT TO WATCH{RESET}")
        if tp > 0:
            lines.append(f"  \u2022 TP fill at {GREEN}{tp}c{RESET} (would close at profit)")
        if hwm - entry >= 10 and hwm - cur_val < 8:
            lines.append(f"  \u2022 Trailing trigger: {YELLOW}bid falling 8c from {hwm}c{RESET}")
        if mand_zone:
            lines.append(f"  \u2022 {RED}Mandatory exit zone{RESET} — <3min and underwater auto-closes")
        else:
            lines.append(f"  \u2022 Session timer crossing 3:00 — triggers end-game logic")
        lines.append(f"  \u2022 Reversal %: currently {rev}% (60%+ = soft reversal)")

    else:
        locked = sess.get("window_locked", False)
        tw = sess.get("trades_this_window", 0)
        bl_set = sess.get("baseline_set", False)
        pr = prs.get("ready", False)

        lines.append(f"{BOLD}POSITION NARRATIVE{RESET}")
        lines.append(f"{DIM}Flat — no position open.{RESET}")
        lines.append("")

        lines.append(f"{BOLD}ENGINE DECISION{RESET}")
        if locked:
            lines.append(f"{YELLOW}Window locked{RESET} — already traded this session. Waiting for the next 15-min window.")
        elif tw >= 1:
            lines.append(f"{YELLOW}Trade cap hit{RESET} — 1/1 trades used this window. Done for now.")
        elif not bl_set:
            smp = sess.get("baseline_samples", 0)
            lines.append(f"{DIM}Collecting baseline{RESET} ({smp}/90s). No entries until baseline locks.")
        elif pr:
            lines.append(f"{GREEN}{BOLD}Entry armed.{RESET} Pressure persistent + confidence high + FVG agrees. Sizing up the order now.")
        elif prs.get("crossed"):
            lines.append(f"{YELLOW}Close to entry{RESET} — pressure crossed threshold, waiting {3-pp_ct} more cycles for persistence.")
        elif abs(fvg) >= 20 and (not pd or pd != ("yes" if fvg > 0 else "no")):
            lines.append(f"{YELLOW}FVG is strong{RESET} but pressure doesn't agree — sitting out the conflict.")
        elif abs(fvg) < fvg_d.get("threshold", 5):
            lines.append(f"{DIM}No divergence yet.{RESET} Fair value hasn't separated enough from baseline to justify an entry.")
        else:
            lines.append(f"{DIM}Scanning{RESET} — conditions assembling but nothing armed yet.")

        # If the Dominant-Direction gate already evaluated and rejected a side,
        # surface the blocker so the operator sees WHY we're flat rather than
        # assuming the engine is idle.
        g = data.get("dominant_gate", {}) or {}
        if g.get("evaluated") and not g.get("qualifies"):
            fails = []
            if not g.get("btc_dominant"):
                fails.append(f"BTC 5m=${g.get('btc_5m_move',0):+.0f}")
            if not g.get("pressure_agrees_strong"):
                fails.append(f"prs {g.get('pressure_direction','—') or '—'}/{g.get('pressure_confidence',0):.0%}")
            if not g.get("rsi_not_contrarian"):
                fails.append(f"RSI {g.get('rsi',50):.0f} contrarian")
            if not g.get("fvg_non_marginal"):
                fails.append(f"FVG marginal {g.get('fvg_magnitude',0)}c")
            if fails:
                lines.append(f"{YELLOW}Dominant-Direction gate blocked:{RESET} {DIM}{' · '.join(fails)}{RESET}.")
        lines.append("")

        lines.append(f"{BOLD}WHAT TO WATCH{RESET}")
        if not bl_set:
            lines.append(f"  \u2022 Baseline samples hitting 90 (starts the scan)")
        else:
            lines.append(f"  \u2022 FVG magnitude crossing {fvg_d.get('threshold', 5)}c (currently {int(abs(fvg))}c)")
            lines.append(f"  \u2022 Pressure score reaching \u00b1{prs.get('threshold', 0.25):.2f} (currently {ps:+.2f})")
            lines.append(f"  \u2022 Persistence 3/3 (currently {pp_ct}/3) — fires entry when met")
        if locked or tw >= 1:
            lines.append(f"  \u2022 Next window opens at the next :00/:15/:30/:45 mark")

        # ── Mid-trade upgrades + missed opportunities (also visible when flat) ──
        up_lines = _narr_upgrades(data)
        if up_lines:
            lines.append("")
            lines.extend(up_lines)

    return lines


def render_narrative(data, stale, age, width, live_bal):
    """Render the narrative tab: session + position in plain English."""
    lines = []
    w = max(width, 70)

    def add(t=""):
        lines.append(_pad(t, w))

    acct = data.get("account", {})
    sess = data.get("session", {})
    bc = live_bal if live_bal > 0 else acct.get("balance_cents", 0)
    bal = bc / 100
    ticker = sess.get("ticker", "")[-18:]
    secs = sess.get("seconds_remaining", 0)
    mins = int(secs) // 60
    sec = int(secs) % 60

    add(_box_top(w))
    add(_box_line(f"{BOLD}ENGINE NARRATIVE{RESET}  {DIM}|{RESET}  {CYAN}{ticker}{RESET}  {DIM}|{RESET}  {BOLD}${bal:.2f}{RESET}  {DIM}|{RESET}  {BOLD}{mins}:{sec:02d}{RESET} left", w))
    add(_box_line(f"{DIM}TAB: cycle view (Dashboard \u2194 Narrative \u2194 Both){RESET}", w))
    add(_box_mid(w))

    # Session narrative
    for line in _narr_session(data, stale, age):
        # Word-wrap long lines
        clean = re.sub(r'\033\[[^m]*m', '', line)
        if len(clean) > w - 4:
            for wrapped in _wrap(line, w - 4):
                add(_box_line(f" {wrapped}", w))
        else:
            add(_box_line(f" {line}", w))

    add(_box_mid(w))

    # Position narrative
    for line in _narr_position(data):
        clean = re.sub(r'\033\[[^m]*m', '', line)
        if len(clean) > w - 4:
            for wrapped in _wrap(line, w - 4):
                add(_box_line(f" {wrapped}", w))
        else:
            add(_box_line(f" {line}", w))

    add(_box_bot(w))
    return "\n".join(lines)


def render(data, log_lines, trades, stats, stale, age, width, live_bal, p_hist, bal_hist):
    lines = []
    w = max(width, 70)

    def add(t=""):
        lines.append(_pad(t, w))

    sess = data.get("session", {})
    acct = data.get("account", {})
    prob = data.get("prob", {})
    btc_d = data.get("btc", {})
    fvg_d = data.get("fvg", {})
    tape = data.get("tape", {})
    prs = data.get("pressure", {})
    pos = data.get("position", {})

    ticker = sess.get("ticker", "")[-18:]
    secs = sess.get("seconds_remaining", 0)
    bc = live_bal if live_bal > 0 else acct.get("balance_cents", 0)
    bal = bc / 100
    regime = sess.get("regime", "?")
    progress = (900 - secs) / 900 if secs > 0 else 1.0

    utc_h = datetime.now(timezone.utc).hour
    night = 2 <= utc_h < 12
    mode = f"{DIM}\u263E NIGHT{RESET}" if night else f"{YELLOW}\u2600 DAY{RESET}"
    sz_note = f" {DIM}[half]{RESET}" if night else ""

    reg_map = {"TRENDING": (GREEN, "\u2197"), "MEAN_REVERTING": (CYAN, "\u2194"), "EXPLOSIVE": (RED, "\u26A1")}
    rc, ri = reg_map.get(regime, (DIM, "?"))

    # ======== HEADER BOX ========
    add(_box_top(w))
    add(_box_line(f"{BOLD}BTC BIAS ENGINE{RESET}  {mode}{sz_note}                          {BOLD}${bal:.2f}{RESET}", w))
    add(_box_line(f"{CYAN}{ticker}{RESET}   {BOLD}{_ft(secs)}{RESET} {_hbar(progress, 14, CYAN)}  {rc}{ri} {regime}{RESET}", w))

    # Stats row
    s = stats
    wr = s.get("wr", 0)
    sk = s.get("streak", 0)
    st = s.get("st", "")
    skc = GREEN if st == "W" else RED
    add(_box_line(
        f"{DIM}24h{RESET} {s.get('n',0)} trades  "
        f"{GREEN}{s.get('w',0)}W{RESET}{DIM}/{RESET}{RED}{s.get('l',0)}L{RESET}  "
        f"WR:{BOLD}{wr:.0f}%{RESET}  "
        f"P&L:{_c(s.get('pnl',0))}  "
        f"{skc}{sk}{st}{RESET}", w))
    add(_box_mid(w))

    # ======== BALANCE SPARKLINE ========
    if bal_hist and len(bal_hist) > 2:
        bl = _spark(bal_hist, min(w - 20, 40))
        mn, mx = min(bal_hist), max(bal_hist)
        add(_box_line(f"{DIM}BAL{RESET} ${mn:.0f}{bl}${mx:.0f}", w))

    # ======== STALE ========
    if stale:
        add(_box_line(f"{BG_RED}{BOLD} ENGINE DOWN ({age:.0f}s) {RESET}", w))

    # ======== BTC + PROBABILITY ========
    btc = btc_d.get("price", 0)
    strike = prob.get("strike", 0)
    fv = prob.get("fair_value", 0)
    pp = prob.get("probability", 0) * 100
    misp = prob.get("mispricing", 0)
    edge = prob.get("edge_cents", 0)
    vol = prob.get("volatility", 0)
    pside = prob.get("side", "")

    add(_box_line(f"{DIM}BTC{RESET} {BOLD}${btc:,.2f}{RESET}  {DIM}K{RESET}=${strike:,.2f}  {DIM}vol{RESET}={vol:.1f}%  {DIM}dist{RESET}={btc_d.get('distance_pct',0):.3f}%", w))

    if prob.get("is_ready"):
        pw = min(w - 30, 30)
        pf = int(pp / 100 * pw)
        pbar = ""
        for i in range(pw):
            if i < pf:
                pbar += f"{RED if i < pw//2 else GREEN}{B}{RESET}"
            elif i == pw//2:
                pbar += f"{WHITE}{V}{RESET}"
            else:
                pbar += f"{DIM}{S}{RESET}"
        sd = _sc(pside) if pside else f"{DIM}---{RESET}"
        add(_box_line(f"{DIM}PROB{RESET} {pbar} {BOLD}{pp:.0f}%{RESET} {sd}  fair={fv:.0f}c  edge={_c(edge,'+.0f')}c  misp={_c(misp,'+.0f')}c", w))
    else:
        add(_box_line(f"{DIM}PROB warming...{RESET}", w))

    # ======== COMPUTE ALL DATA UPFRONT ========
    bl_set = sess.get("baseline_set", False)
    bl_price = sess.get("baseline_price", 0)
    fvg_val = fvg_d.get("fvg_vs_baseline", 0)
    fvg_t = fvg_d.get("threshold", 5)
    fvg_x = fvg_d.get("crossed", False)
    mid = tape.get("mid_cents", 0)
    imb = tape.get("taker_imbalance", 0)
    vwap = tape.get("vwap_cents", 0)
    ps = prs.get("score", 0)
    pd = prs.get("direction", "")
    pc = prs.get("confidence", 0)
    pt = prs.get("threshold", 0.25)
    pp_ct = prs.get("persist_count", 0)
    pr = prs.get("ready", False)
    imp = prs.get("btc_impulse", 0)
    bk = prs.get("book_pressure", 0)
    fl = prs.get("flow_momentum", 0)
    lg = prs.get("kalshi_lag", 0)
    ta_d = data.get("ta", {})
    ta_dir = ta_d.get("direction", "?").upper()
    ta_conf = ta_d.get("confidence", 0)
    rsi = ta_d.get("rsi", 0)
    adx = ta_d.get("adx", 0)
    tdc = GREEN if ta_dir == "UP" else RED if ta_dir == "DOWN" else DIM
    five_s = data.get("five_sec", {})
    tv = five_s.get("tick_velocity", 0)
    bb_u = five_s.get("bb_upper", 0)
    bb_m = five_s.get("bb_mid", 0)
    bb_l = five_s.get("bb_lower", 0)
    mins_t = int(secs) // 60
    sec_t = int(secs) % 60
    timer_c = GREEN if secs > 600 else YELLOW if secs > 180 else RED
    timer_str = f"{timer_c}{BOLD}{mins_t:02d}:{sec_t:02d}{RESET}"

    # ======== TWO-COLUMN LAYOUT ========
    # Left: Engine State + Gates | Right: Signal Instruments
    # Build each column as list of strings, then merge

    left_w = (w - 5) // 2 + 2   # left column wider by 2
    right_w = w - 5 - left_w      # right gets the remainder

    def _trunc(text, maxw):
        """Truncate text to maxw visible chars, preserving ANSI."""
        clean = re.sub(r'\033\[[^m]*m', '', text)
        if len(clean) <= maxw:
            return text
        # Walk through text, counting visible chars
        vis = 0
        i = 0
        while i < len(text) and vis < maxw:
            if text[i] == '\033':
                end = text.find('m', i)
                if end >= 0:
                    i = end + 1
                    continue
            vis += 1
            i += 1
        return text[:i] + RESET

    def _dual(left, right):
        """Merge two column strings into one box line with center divider."""
        left = _trunc(left, left_w)
        right = _trunc(right, right_w)
        cl = re.sub(r'\033\[[^m]*m', '', left)
        cr = re.sub(r'\033\[[^m]*m', '', right)
        lpad = max(0, left_w - len(cl))
        rpad = max(0, right_w - len(cr))
        return f"{DIM}{V}{RESET} {left}{' '*lpad}{DIM}{V}{RESET}{right}{' '*rpad}{DIM}{V}{RESET}"

    add(f"{DIM}{LJ}{HL*(left_w+1)}{TJ}{HL*(right_w+1)}{RJ}{RESET}")

    # ---- ROW: Headers ----
    add(_dual(f" {BOLD}\u25A0 ENGINE{RESET}  {timer_str}", f" {BOLD}SIGNALS{RESET}"))

    # ---- LEFT: Engine state / gates ----
    # ---- RIGHT: FVG, Pressure, Tape, BB ----

    # Build right-side lines
    R = []  # right column lines
    bw = min(right_w - 10, 16)  # bar width — leave room for labels

    # -- FVG BASELINE --
    R.append(f" {BOLD}FVG{RESET}")
    if bl_set:
        fb = _cbar(fvg_val, bw, 30)
        fstat = f"{GREEN}\u2713{RESET}" if fvg_x else f"{DIM}{int(abs(fvg_val)/max(fvg_t,1)*100)}%{RESET}"
        R.append(f" {DIM}NO{RESET}{fb}{DIM}YES{RESET} {_c(fvg_val,'+.0f')}c/{fvg_t}c {fstat}")
        R.append(f" base={bl_price}c  samp={sess.get('baseline_samples',0)}")
    else:
        R.append(f" {YELLOW}collecting{RESET} {sess.get('baseline_samples',0)}/90s")
        R.append(f" {_hbar(min(sess.get('baseline_samples',0)/90,1), bw, YELLOW)}")

    # -- PRESSURE --
    R.append(f" {BOLD}PRESSURE{RESET}")
    pgauge = _cbar(ps, bw, 0.6)
    if pr:
        pstat = f"{BG_GREEN}{BOLD} {pd.upper()} \u2713 {RESET}"
    elif prs.get("crossed"):
        pstat = f"{YELLOW}{pp_ct}/3{RESET}"
    else:
        pstat = f"{DIM}{int(abs(ps)/max(pt,0.01)*100)}%{RESET}"
    R.append(f" {DIM}NO{RESET}{pgauge}{DIM}YES{RESET} {_c(ps,'+.2f')} {pstat}")
    R.append(f" conf={pc:.0%} thr={pt:.2f}")

    # Components on two lines
    cbw = max(4, (right_w - 20) // 2)  # component bar width
    def _mc(nm, v):
        bar = _hbar(abs(v), cbw, GREEN if v > 0 else RED if v < 0 else DIM)
        return f"{DIM}{nm}{RESET}{bar}"
    R.append(f" {_mc('I',imp)} {_mc('B',bk)} {_mc('F',fl)} {_mc('L',lg)}")

    # BTC move windows (drives Dominant-Direction gate: 5m ±$30 threshold)
    b5s = prs.get("btc_move_5s", 0)
    b30s = prs.get("btc_move_30s", 0)
    b5m = prs.get("btc_move_300s", 0)
    b5m_ok = abs(b5m) >= 30
    b5m_tag = f"{GREEN}\u2713{RESET}" if b5m_ok else f"{DIM}{int(abs(b5m)/30*100)}%{RESET}"
    R.append(f" {DIM}BTC{RESET} 5s{_c(b5s,'+.0f')} 30s{_c(b30s,'+.0f')} 5m{_c(b5m,'+.0f')} {b5m_tag}")

    # Sparkline
    if p_hist and len(p_hist) > 3:
        R.append(f" {_spark(p_hist, min(right_w - 3, 30))}")
    else:
        R.append(f" {DIM}...{RESET}")

    # -- TAPE --
    mid_bar = _hbar(mid/100, bw, GREEN if mid > 55 else RED if mid < 45 else WHITE)
    R.append(f" {DIM}TAPE{RESET} {BOLD}{int(mid)}c{RESET}{mid_bar} im={_c(imb,'+.1f')}")

    # -- TA + TICK --
    kst = ta_d.get("kst_bullish", False)
    kst_s = f"{GREEN}b{RESET}" if kst else f"{RED}b{RESET}"
    R.append(f" {DIM}TA{RESET} {tdc}{ta_dir}{RESET} c={int(ta_conf)} r={rsi:.0f} a={adx:.0f} {kst_s}")
    tv_bar = _cbar(tv / 20, bw // 2, 1.0)
    R.append(f" {DIM}VEL{RESET}{_c(tv,'+.1f')}{tv_bar} 5s={'ok' if five_s.get('is_ready') else 'warm'}")

    if bb_u > 0 and bb_l > 0 and bb_u != bb_l:
        bb_pos = max(0, min(1, (btc - bb_l) / (bb_u - bb_l)))
        bbw_v = min(right_w - 10, 16)
        bb_bar = list(f"{DIM}{S}{RESET}" for _ in range(bbw_v))
        for i in range(bbw_v):
            fr = i / bbw_v
            if fr < 0.2: bb_bar[i] = f"{GREEN}{S}{RESET}"
            elif fr > 0.8: bb_bar[i] = f"{RED}{S}{RESET}"
        bb_bar[bbw_v // 2] = f"{WHITE}{HL}{RESET}"
        bi = max(0, min(bbw_v - 1, int(bb_pos * (bbw_v - 1))))
        bb_bar[bi] = f"{BOLD}{YELLOW}\u2666{RESET}"
        R.append(f" {BOLD}BB{RESET} {''.join(bb_bar)} w=${bb_u-bb_l:.0f}")
    else:
        R.append(f" {BOLD}BB{RESET} {DIM}warming...{RESET}")

    # Build left-side lines
    L = []

    if pos.get("active"):
        side = pos.get("side", "")
        ct = pos.get("count", 0)
        entry = pos.get("entry_cents", 0)
        tp = pos.get("tp_price", 0)
        hwm = pos.get("high_water", 0)
        mae = pos.get("low_water", 0)
        unreal = pos.get("unrealized_cents", 0) / 100
        rev = pos.get("reversal_pct", 0)
        ep = pos.get("entry_prob", 50)
        cp = pos.get("current_prob", 50)
        pnl_per = (int(mid) - entry) if side == "yes" else ((100-int(mid)) - entry)
        pnl_bar = _hbar(max(0, pnl_per/max(tp-entry,10)), 10, GREEN if pnl_per > 0 else RED)
        revc = RED if rev >= 60 else YELLOW if rev >= 40 else GREEN

        # Exit mode badge
        _exit_mode = pos.get("exit_mode", "SCALP")
        _exec_path = pos.get("exec_path", "")
        _orig_ct = pos.get("original_count", ct)
        _orig_entry = pos.get("original_entry", entry)
        _dca_fired = pos.get("scalp_dca_fired", False)
        if _exit_mode == "HOLD_EXPIRY":
            _mode_badge = f"{BG_GREEN}{BOLD} HOLD \u2192 EXPIRY {RESET}"
        else:
            _mode_badge = f"{CYAN} SCALP {RESET}"

        L.append(f" {BOLD}\u25B6{RESET} {_mode_badge} {DIM}prob{RESET}={BOLD}{pp:.0f}%{RESET}")
        L.append(f" {_sc(side)} {ct}x @ {entry}c")
        if _dca_fired:
            L.append(f" {YELLOW}DCA'd{RESET} from {_orig_ct}x@{_orig_entry}c")
        # Show tiered TP for HOLD or single TP for SCALP
        if _exit_mode == "HOLD_EXPIRY":
            L.append(f" TPs: {DIM}78c{RESET}/{DIM}85c{RESET}/{DIM}95c{RESET}/{GREEN}100c{RESET}")
        else:
            L.append(f" {_c(unreal)}  TP={tp}c")
        if "CONVICTION_DIP" in _exec_path:
            L.append(f" {MAGENTA}\u25C6 DIP{RESET} {_exec_path}")
        elif "BOOST" in _exec_path:
            L.append(f" {MAGENTA}\u26A1{_exec_path}{RESET}")
        L.append(f"")

        # Trade progression bar: entry to TP range
        # Red zone = loss, green zone = profit, marker = current
        bar_w = max(left_w - 4, 16)
        loss_max = entry  # max loss in cents (contract goes to 0)
        tp_gain = tp - entry if tp > entry else 20
        total_range = loss_max + tp_gain
        # Map current price to bar position
        if side == "yes":
            cur_val = int(mid)  # YES value = mid
        else:
            cur_val = 100 - int(mid)  # NO value = 100 - mid
        # Position in range: 0 = total loss, loss_max = entry, total_range = TP
        bar_pos = (cur_val) / 100  # normalize 0-100c to 0-1
        entry_pos = entry / 100
        tp_pos = tp / 100

        prog_bar = []
        for i in range(bar_w):
            frac = i / bar_w
            if frac < entry_pos:
                # Below entry = loss zone
                prog_bar.append(f"{RED}{S}{RESET}")
            elif frac < tp_pos:
                # Between entry and TP = profit zone
                prog_bar.append(f"{GREEN}{S}{RESET}")
            else:
                # Above TP = beyond target
                prog_bar.append(f"{DIM}{S}{RESET}")

        # Entry marker
        ei = max(0, min(bar_w - 1, int(entry_pos * bar_w)))
        prog_bar[ei] = f"{WHITE}[{RESET}"
        # TP marker
        ti = max(0, min(bar_w - 1, int(tp_pos * bar_w)))
        prog_bar[ti] = f"{WHITE}]{RESET}"
        # Current price marker
        ci = max(0, min(bar_w - 1, int(bar_pos * bar_w)))
        if pnl_per >= 0:
            prog_bar[ci] = f"{GREEN}{BOLD}\u2666{RESET}"
        else:
            prog_bar[ci] = f"{RED}{BOLD}\u2666{RESET}"
        # HWM marker
        hwm_pos = hwm / 100 if side == "yes" else (100 - hwm) / 100
        hi = max(0, min(bar_w - 1, int(hwm_pos * bar_w)))
        if hi != ci and hi != ei and hi != ti:
            prog_bar[hi] = f"{CYAN}\u2502{RESET}"

        L.append(f" {''.join(prog_bar)}")
        L.append(f" {DIM}[{RESET}=ent {GREEN}\u2666{RESET}=now {CYAN}|{RESET}=hwm {DIM}]{RESET}=tp")
        L.append(f"")

        # Position stats
        L.append(f" HWM={hwm}c MAE={mae}c")
        L.append(f" prob {ep:.0f}%\u2192{cp:.0f}%")
        L.append(f" {revc}reversal={rev}%{RESET}")
        L.append(f"")

        # Engine state
        cycle_icon = f"{DIM}\u263E{RESET}" if night else f"{YELLOW}\u2600{RESET}"
        cycle_label = "NIGHT" if night else "DAY"
        sz_label = f"{DIM}half{RESET}" if night else f"full"
        L.append(f" {GREEN}\u2022 LIVE{RESET} {cycle_icon}{cycle_label} {sz_label}")

        trail_armed = hwm - entry >= 10
        trail_give = hwm - cur_val if trail_armed else 0
        thesis_ok = not ((fvg_val < 0 and side == "yes") or (fvg_val > 0 and side == "no"))
        mand_zone = secs < 180

        # Determine current engine action (mode-aware)
        if _exit_mode == "HOLD_EXPIRY":
            if mand_zone:
                eng_status = f"{MAGENTA}FINAL MINUTES{RESET}"
                eng_reason = f"hold-to-expiry, settling at 100 or 0"
            elif pnl_per > 20:
                eng_status = f"{GREEN}{BOLD}DEEP PROFIT{RESET}"
                eng_reason = f"+{pnl_per}c, tiered TPs filling"
            elif pnl_per > 0:
                eng_status = f"{GREEN}PROFITABLE{RESET}"
                eng_reason = f"+{pnl_per}c, holding for 78c+ tier"
            elif pnl_per > -10:
                eng_status = f"{CYAN}HOLDING THESIS{RESET}"
                eng_reason = f"{pnl_per:+d}c, no stop — riding to settle"
            else:
                eng_status = f"{YELLOW}ADVERSE{RESET}"
                eng_reason = f"{pnl_per:+d}c, conviction hold (no stop)"
        else:  # SCALP mode
            if trail_armed and trail_give >= 8:
                eng_status = f"{YELLOW}TRAIL TRIGGERED{RESET}"
                eng_reason = f"give-back {trail_give}c >= 8c"
            elif mand_zone and cur_val < entry:
                prob_favors = (pp > 55 and side == "yes") or (pp < 45 and side == "no")
                if prob_favors:
                    eng_status = f"{MAGENTA}HOLDING TO SETTLE{RESET}"
                    eng_reason = f"<3min but prob={pp:.0f}% favors"
                else:
                    eng_status = f"{RED}EXIT ZONE{RESET}"
                    eng_reason = f"<3min, underwater, no edge"
            elif pnl_per > 0:
                eng_status = f"{GREEN}PROFITABLE{RESET}"
                eng_reason = f"+{pnl_per}c, waiting for TP fill"
            elif pnl_per > -5:
                eng_status = f"{DIM}NEAR ENTRY{RESET}"
                eng_reason = f"{pnl_per:+d}c, scalp mode (stop@-10c)"
            else:
                eng_status = f"{RED}UNDERWATER{RESET}"
                eng_reason = f"{pnl_per:+d}c, stop active at {entry-10}c"

        L.append(f" {eng_status}")
        L.append(f" {DIM}{eng_reason}{RESET}")
        L.append(f"")

        # Exit gate details
        tr_c = GREEN if trail_armed else DIM
        th_c = GREEN if thesis_ok else RED
        mn_c = RED if mand_zone else GREEN
        # Refinement B: if trail is being held by strong pressure + tight book,
        # show it as a "HELD" badge so the operator knows why the trail didn't
        # fire on the give-back.
        _held_n = int(pos.get("_trail_held_count", 0) or 0)
        if _held_n > 0 and trail_armed:
            L.append(f" {MAGENTA}trail:HELD {_held_n}c{RESET} {DIM}prs+tight{RESET}")
        else:
            L.append(f" {tr_c}trail:{'armed +'+str(hwm-entry)+'c' if trail_armed else 'need +10c'}{RESET}")
        L.append(f" {th_c}thesis:{'intact' if thesis_ok else 'DEAD'}{RESET}")
        L.append(f" {mn_c}mand:{'<3MIN' if mand_zone else str(int(secs))+'s'}{RESET}")
        L.append(f" dca={pos.get('dca_tiers',0)}/3")

        # ── Per-trade ENTRY FACTORS (why we took THIS trade) ─────────────
        ef = pos.get("entry_factors", {}) or {}
        if ef:
            L.append(f"")
            L.append(f" {BOLD}ENTRY FACTORS{RESET}")
            eps = ef.get("pressure_score", 0.0)
            epc = ef.get("pressure_conf", 0.0)
            epsc = GREEN if eps > 0 else RED if eps < 0 else DIM
            L.append(f" prs {epsc}{eps:+.2f}{RESET} @ {epc:.0%} conf")
            eI = ef.get("btc_impulse", 0.0); eB = ef.get("book_pressure", 0.0)
            eF = ef.get("flow_momentum", 0.0); eL = ef.get("kalshi_lag", 0.0)
            L.append(f" I{_c(eI,'+.2f')} B{_c(eB,'+.2f')}")
            L.append(f" F{_c(eF,'+.2f')} L{_c(eL,'+.2f')}")
            eb5 = ef.get("btc_5m_move", 0.0)
            ersi = ef.get("rsi", 50.0)
            efvg = ef.get("fvg_signed", 0)
            L.append(f" BTC5m{_c(eb5,'+.0f')} RSI={ersi:.0f}")
            L.append(f" FVG{_c(efvg,'+d')}c pers={ef.get('persistence',0)}/3")
            eg = pos.get("entry_gate", {}) or {}
            if eg.get("qualifies"):
                L.append(f" {GREEN}\u2713 DOMINANT ENTRY{RESET}")
            elif eg:
                L.append(f" {YELLOW}non-dominant path{RESET}")
            # Refinement A: surface whether cap-widen was active at entry
            # (microstructure source + pressure conf >= 0.75 means [10-95] range)
            _src = ef.get("direction_source", "") or ""
            if "microstructure" in _src and epc >= 0.75:
                L.append(f" {MAGENTA}\u2195 CAP WIDEN{RESET} {DIM}[10-95]{RESET}")

        # ── Missed dominant opportunities + recent upgrades (during trade) ─
        # Shown while holding so the operator can see whether the gate has
        # re-qualified mid-trade (and whether we acted on it).
        md_a = data.get("missed_dominant", {}) or {}
        ru_a = data.get("recent_upgrades", []) or []
        ms_a = int(md_a.get("session", 0))
        mt_a = int(md_a.get("total", 0))
        upg_done_a = bool(md_a.get("upgrade_fired_this_window", False))
        if ms_a or mt_a or ru_a or upg_done_a:
            import time as _tm_a
            L.append(f"")
            L.append(f" {BOLD}MID-TRADE{RESET}")
            ms_col_a = GREEN if ms_a == 0 else YELLOW if ms_a < 3 else RED
            upg_tag_a = f" {GREEN}\u2191FIRED{RESET}" if upg_done_a else ""
            L.append(f" {DIM}missed:{RESET} {ms_col_a}{ms_a}{RESET}{DIM}w{RESET} "
                     f"{DIM}{mt_a}t{RESET}{upg_tag_a}")
            if ru_a:
                for evt in ru_a[-2:]:
                    kind = evt.get("kind", "")
                    side_e = (evt.get("side","") or "").upper()
                    sc = GREEN if side_e == "YES" else RED if side_e == "NO" else DIM
                    age_s = int(max(0, _tm_a.time() - evt.get("ts", 0)))
                    age_tag = f"{age_s}s" if age_s < 60 else f"{age_s // 60}m"
                    if kind == "size_upgrade":
                        L.append(f" {sc}\u2191{RESET} +{evt.get('added_ct',0)}ct@{evt.get('price',0)}c {DIM}{age_tag}{RESET}")
                    elif kind == "exit_upgrade":
                        L.append(f" {sc}\u2191{RESET} scalp\u2192hold {DIM}{age_tag}{RESET}")

    else:
        locked = sess.get("window_locked", False)
        tw = sess.get("trades_this_window", 0)

        def _g(ok, nm):
            return f" {GREEN}\u2713{RESET} {nm}" if ok else f" {RED}\u2717{RESET} {nm}"

        g_baseline = bl_set
        g_fvg = fvg_x
        g_pdir = pd == ("yes" if fvg_val > 0 else "no") if pd and fvg_val != 0 else False
        g_pready = pr
        g_sfvg = abs(fvg_val) >= 20
        g_band = 35 <= int(mid) <= 75
        g_open = not locked
        g_cap = tw < 1

        L.append(f" {BOLD}NO POSITION{RESET}  {DIM}prob{RESET}={BOLD}{pp:.0f}%{RESET}")
        L.append(f"")

        # Position bar showing where mid is in the entry band
        bar_w = max(left_w - 4, 16)
        prog_bar = []
        for i in range(bar_w):
            frac = i / bar_w * 100  # 0-100c
            if 35 <= frac <= 75:
                prog_bar.append(f"{GREEN}{S}{RESET}")  # entry band
            else:
                prog_bar.append(f"{DIM}{S}{RESET}")  # outside band
        # Mark entry band boundaries
        lo_i = max(0, min(bar_w-1, int(35/100*bar_w)))
        hi_i = max(0, min(bar_w-1, int(75/100*bar_w)))
        prog_bar[lo_i] = f"{WHITE}[{RESET}"
        prog_bar[hi_i] = f"{WHITE}]{RESET}"
        # Mark current mid
        mi = max(0, min(bar_w-1, int(int(mid)/100*bar_w)))
        in_band = 35 <= int(mid) <= 75
        prog_bar[mi] = f"{GREEN if in_band else RED}{BOLD}\u2666{RESET}"
        L.append(f" {''.join(prog_bar)}")
        L.append(f" {DIM}[{RESET}=35c {GREEN}\u2666{RESET}=mid({int(mid)}c) {DIM}]{RESET}=75c")
        L.append(f"")

        # Engine state: live status + cycle + reason
        cycle_icon = f"{DIM}\u263E{RESET}" if night else f"{YELLOW}\u2600{RESET}"
        cycle_label = "NIGHT" if night else "DAY"
        sz_label = f"{DIM}half{RESET}" if night else f"full"

        if stale:
            L.append(f" {RED}\u25CF DOWN{RESET} {cycle_icon}{cycle_label}")
            L.append(f" {DIM}no data for {int(age)}s{RESET}")
        elif locked:
            L.append(f" {YELLOW}\u25CF LOCKED{RESET} {cycle_icon}{cycle_label} {sz_label}")
            L.append(f" {DIM}traded this window{RESET}")
        elif tw >= 1:
            L.append(f" {YELLOW}\u25CF CAPPED{RESET} {cycle_icon}{cycle_label} {sz_label}")
            L.append(f" {DIM}1/1 trades used{RESET}")
        else:
            L.append(f" {GREEN}\u25CF LIVE{RESET} {cycle_icon}{cycle_label} {sz_label}")

            if not g_baseline:
                smp = sess.get("baseline_samples", 0)
                L.append(f" {YELLOW}WARMUP{RESET} {DIM}baseline {smp}{RESET}")
            elif not g_fvg:
                pct = int(abs(fvg_val)/max(fvg_t,1)*100)
                L.append(f" {DIM}SCANNING{RESET} FVG {pct}%")
            elif g_sfvg and not g_pdir:
                L.append(f" {YELLOW}CONFLICT{RESET}")
                L.append(f" {DIM}FVG={'yes' if fvg_val>0 else 'no'} prs={pd or '-'}{RESET}")
            elif not g_pready:
                if prs.get("crossed"):
                    L.append(f" {YELLOW}CONFIRMING{RESET} {pp_ct}/3")
                else:
                    p_pct_t = int(abs(ps)/max(pt,0.01)*100)
                    L.append(f" {DIM}BUILDING{RESET} prs {p_pct_t}%")
            elif not g_band:
                L.append(f" {RED}OUT OF BAND{RESET}")
                L.append(f" {DIM}mid={int(mid)}c [35-75]{RESET}")
            else:
                L.append(f" {GREEN}{BOLD}READY TO ENTER{RESET}")
        L.append(f"")

        # Gates
        L.append(f" {DIM}GATES:{RESET}")
        L.append(_g(g_baseline, "baseline"))
        L.append(_g(g_fvg, f"fvg {_c(fvg_val,'+.0f')}c"))
        if g_sfvg:
            L.append(_g(True, "strong fvg"))
            L.append(_g(g_pdir, "prs agrees"))
        else:
            L.append(_g(g_pready, f"prs {ps:+.2f}"))
            L.append(_g(g_pdir, "prs agrees"))
        L.append(_g(g_band, f"band {int(mid)}c"))
        L.append(_g(g_open, f"open {tw}/1"))
        L.append(_g(prob.get("is_ready", False), "prob"))

        # ── Dominant-Direction gate (4-factor AND-filter) ────────────────
        gate = data.get("dominant_gate", {}) or {}
        if gate.get("evaluated"):
            gside = (gate.get("side","") or "").upper() or "—"
            gsc = GREEN if gside == "YES" else RED if gside == "NO" else DIM
            q = gate.get("qualifies", False)
            tag = f"{BG_GREEN}{BOLD} PASS {RESET}" if q else f"{BG_RED}{BOLD} BLOCK {RESET}"
            L.append(f"")
            L.append(f" {BOLD}DOMINANT{RESET} {gsc}{gside}{RESET} {tag}")
            L.append(_g(gate.get("btc_dominant",False), f"btc5m {gate.get('btc_5m_move',0):+.0f}"))
            L.append(_g(gate.get("pressure_agrees_strong",False),
                        f"prs {(gate.get('pressure_direction','') or '—').upper()} {gate.get('pressure_confidence',0):.0%}"))
            L.append(_g(gate.get("rsi_not_contrarian",False), f"rsi {gate.get('rsi',50):.0f}"))
            L.append(_g(gate.get("fvg_non_marginal",False), f"fvg |{gate.get('fvg_magnitude',0)}|c"))

        # ── Missed dominant opportunities + recent upgrades ─────────────
        md = data.get("missed_dominant", {}) or {}
        ms = int(md.get("session", 0))
        mt = int(md.get("total", 0))
        upg_done = bool(md.get("upgrade_fired_this_window", False))
        if ms or mt or upg_done:
            L.append(f"")
            # Color the session counter: green if 0, yellow if >0 (real cost)
            ms_col = GREEN if ms == 0 else YELLOW if ms < 3 else RED
            upg_tag = f" {GREEN}\u2191upgraded{RESET}" if upg_done else ""
            L.append(f" {DIM}MISSED:{RESET} {ms_col}{ms}{RESET}{DIM}/win{RESET} "
                     f"{DIM}{mt}{RESET}{DIM}/tot{RESET}{upg_tag}")

        ru = data.get("recent_upgrades", []) or []
        if ru:
            L.append(f" {DIM}UPGRADES:{RESET}")
            # Show the most recent 2 — oldest first, so newest is closest to
            # the present on the next line.
            for evt in ru[-2:]:
                kind = evt.get("kind", "")
                side = (evt.get("side", "") or "").upper()
                sc = GREEN if side == "YES" else RED if side == "NO" else DIM
                age_s = int(max(0, __import__("time").time() - evt.get("ts", 0)))
                age_tag = f"{age_s}s" if age_s < 60 else f"{age_s // 60}m"
                if kind == "size_upgrade":
                    desc = f"+{evt.get('added_ct',0)}ct@{evt.get('price',0)}c"
                elif kind == "exit_upgrade":
                    desc = f"scalp\u2192hold {evt.get('count',0)}ct"
                else:
                    desc = kind
                L.append(f"  {sc}{side}{RESET} {desc} {DIM}({age_tag} ago){RESET}")

        fl_d = data.get("flow", {})
        if fl_d.get("direction", ""):
            L.append(f"")
            L.append(f" {DIM}flow:{RESET} {fl_d['direction'].upper()} {fl_d.get('conviction',0):.0f}%")

    # Merge columns — pad to same height
    max_rows = max(len(L), len(R))
    while len(L) < max_rows:
        L.append("")
    while len(R) < max_rows:
        R.append("")

    for l_line, r_line in zip(L, R):
        add(_dual(l_line, r_line))

    add(_box_mid(w))

    # ======== TRADES TABLE ========
    add(_box_line(f"{BOLD}RECENT TRADES{RESET}", w))
    add(_box_line(f" {DIM}time  side  ct  entry  pnl       status{RESET}", w))
    if trades:
        for t in trades[:8]:
            ts = t.get("placed_at", "")
            ts = ts[11:16] if len(ts) > 10 else "?"
            sd = t.get("side", "?")
            ct = t.get("filled_count") or t.get("count", 0)
            px = t.get("limit_price", 0)
            pnl = t.get("pnl", 0) or 0
            status_raw = t.get("status", "")
            status_map = {"reconciled_settled": "settled", "exited_loss": "loss", "exited_win": "win",
                          "thesis_exit": "thesis", "reversal_exit": "reversal", "won": "won"}
            status = status_map.get(status_raw, status_raw[:8])
            ic = "\u2713" if pnl > 0 else "\u2717" if pnl < 0 else "\u2500"
            sc = GREEN if pnl > 0 else RED if pnl < 0 else DIM
            sdc = f"{GREEN}Y{RESET}" if sd == "yes" else f"{RED}N{RESET}"
            add(_box_line(f" {DIM}{ts}{RESET}  {sdc}    {ct:2d}  {px:3d}c   {sc}{ic} ${pnl:+.2f}{RESET}  {DIM}{status}{RESET}", w))
    else:
        add(_box_line(f" {DIM}no trades yet{RESET}", w))

    add(_box_mid(w))

    # ======== DECISION LOG ========
    add(_box_line(f"{BOLD}ENGINE LOG{RESET}", w))
    if log_lines:
        for line in log_lines[-6:]:
            try:
                parts = line.split("] ", 1)
                if len(parts) > 1:
                    msg = parts[1]
                    tp_str = line[11:19] if len(line) > 19 else ""
                    if ": " in msg:
                        msg = msg.split(": ", 1)[1]
                    # Highlight keywords — order matters: more-specific phrases
                    # first so they don't get half-colored by a substring match.
                    for kw, kc in [("ESCROW SWEEP", MAGENTA), ("ESCROW LEAK", RED),
                                   ("PRESSURE CAP WIDEN", MAGENTA),
                                   ("CONVICTION_DIP FIRED", MAGENTA),
                                   ("CONVICTION_DIP", MAGENTA),
                                   ("TRAIL RELEASE", YELLOW), ("TRAIL HOLD", GREEN),
                                   ("HOLD_EXPIRY TP", GREEN), ("SCALP TP", CYAN),
                                   ("CONVICTION BOOST", MAGENTA), ("SCALP DCA", YELLOW),
                                   ("SCALP STOP", RED), ("PEAK GIVE", YELLOW),
                                   ("PRESSURE ENTRY", GREEN), ("FILL:", GREEN),
                                   ("SELL TIER", CYAN), ("SELL LADDER", CYAN),
                                   ("MISS", RED), ("EXIT", YELLOW), ("HOLD", MAGENTA),
                                   ("REVERSAL-RISK", RED), ("REGIME SKIP", RED),
                                   ("WINDOW LOCKED", YELLOW), ("TRAIL", YELLOW)]:
                        msg = msg.replace(kw, f"{kc}{kw}{RESET}")
                    display = f" {DIM}{tp_str}{RESET} {msg}"
                else:
                    display = f" {line}"
            except Exception:
                display = f" {line}"
            clean = re.sub(r'\033\[[^m]*m', '', display)
            if len(clean) > w - 4:
                over = len(clean) - (w - 4)
                display = display[:len(display) - over]
            add(_box_line(display, w))

    # ======== FOOTER ========
    ag = f"{age:.1f}s" if age < 100 else f"{int(age)}s"
    ac = GREEN if age < 5 else YELLOW if age < 10 else RED

    # Scan recent log lines for escrow / refinement events so the operator
    # sees them without needing to find them in the scrolling log tail.
    _esc_tag = ""
    if log_lines:
        for _line in reversed(log_lines[-40:]):
            if "ESCROW SWEEP" in _line:
                _esc_tag = f"  {MAGENTA}\u26A0 ESCROW SWEPT{RESET}"
                break
            if "ESCROW LEAK" in _line:
                _esc_tag = f"  {RED}\u26A0 ESCROW LEAK{RESET}"
                break

    add(_box_line(f"{ac}\u2022{RESET} {ag}  {DIM}DOMINANT gate: BTC5m ±$30 \u00b7 prs aligned \u00b7 RSI 30-70 \u00b7 FVG \u226510c  |  TAB: narrative \u00b7 Q: quit{RESET}{_esc_tag}", w))
    add(_box_bot(w))

    return "\n".join(lines)


def _today_perf_breakdown():
    """Query trades.db for today's per-tier P&L summary.

    2026-04-28 (Claude #7): rewrote to use settlement_ledger as the
    authoritative source for SETTLED tickers, falling back to
    kalshi_trades.pnl only for still-active positions. The kalshi_trades
    pnl column is known to be unreliable — see scripts/reconcile_pnl.py
    docstring for the forensic. Per-ticker settlement P&L attributed to
    strategy via the join.

    Returns dict: {tier: {n, w, l, wr, pnl, avg_win, avg_loss, total_ct}}
    Tiers: TA_FORCED, LATE_DOMINANT, ARB, SR_FADE, OTHER
    Plus a separate "MANUAL" entry for captured manual fills.
    """
    out = {}
    try:
        conn = sqlite3.connect(DB_PATH, timeout=2)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        # ── Step 1: pull today's engine fills + group them by ticker so we
        # can later attribute settlement P&L back to a strategy name.
        # 2026-04-28 (Claude #7 follow-up): two bugs fixed here.
        #   a) placed_at is ISO-8601 with 'T' separator; comparing it as a
        #      string against datetime('now',...) (which uses ' ' separator)
        #      sorted ALL T-stamped rows above any space-separated cutoff,
        #      i.e. the cutoff was effectively ignored. Wrap placed_at in
        #      datetime() to normalize.
        #   b) PT-day window: PT-today starts at 07:00 UTC (PDT), not at
        #      16:00 UTC as the original '-8 hours' calc produced. The
        #      correct shift is `'-7 hours','start of day','+7 hours'`:
        #      first shift now to PT, take start of PT day, then shift the
        #      result back to its UTC representation.
        # Note: kalshi_trades.filled_count is NULL until settlement runs
        # log_kalshi_outcome — so we filter on `count` (set at INSERT to
        # order.filled_count) and alias it back to `filled_count` for the
        # downstream code that reads r["filled_count"].
        c.execute(
            """
            SELECT ticker, strategy_name, side, count AS filled_count,
                   limit_price, status
            FROM kalshi_trades
            WHERE count > 0
              AND datetime(placed_at) > datetime('now','-7 hours','start of day','+7 hours')
            """
        )
        engine_rows = [dict(r) for r in c.fetchall()]
        # Build ticker → list of (strategy, side, ct, px) for attribution
        engine_by_ticker: dict = {}
        for r in engine_rows:
            engine_by_ticker.setdefault(r["ticker"], []).append(r)

        # ── Step 2: for each settled ticker today, look up settlement_ledger
        # and attribute P&L to whichever strategy entered it.
        c.execute(
            """
            SELECT ticker, market_result, yes_count, no_count,
                   yes_total_cost, no_total_cost, fee_cents, pnl_cents,
                   settled_ts_ms
            FROM settlement_ledger
            WHERE settled_ts_ms > strftime('%s','now','-7 hours','start of day','+7 hours') * 1000
            """
        )
        settlements = {r["ticker"]: dict(r) for r in c.fetchall()}

        # ── Step 3: walk engine fills, compute per-row P&L from
        # settlement truth (not kalshi_trades.pnl). Group by tier.
        def _classify(sn):
            up = (sn or "").upper()
            if "LATE_DOMINANT" in up: return "LATE_DOMINANT"
            if "TA_FORCED" in up or "TA-FORCED" in up: return "TA_FORCED"
            if "ARB" in up: return "ARB"
            if "SR_FADE" in up: return "SR_FADE"
            return "OTHER"

        for r in engine_rows:
            tier = _classify(r["strategy_name"])
            d = out.setdefault(tier, {"n": 0, "w": 0, "l": 0,
                                       "pnl": 0.0, "wins": [], "losses": [],
                                       "ct": 0, "open": 0})
            d["n"] += 1
            d["ct"] += int(r["filled_count"] or 0)

            # Compute realized pnl from settlement truth
            settle = settlements.get(r["ticker"])
            if settle is None:
                # Not settled yet — open position
                d["open"] += 1
                continue
            won = (r["side"] or "").lower() == (settle["market_result"] or "").lower()
            settle_cents = 100 if won else 0
            limit_px = int(r["limit_price"] or 0)
            ct = int(r["filled_count"] or 0)
            # Per-fill gross P&L = (settle - entry_price) * ct / 100
            gross = (settle_cents - limit_px) * ct / 100.0
            # Fee allocation: split ticker-level fee proportionally by ct
            yes_ct = float(settle.get("yes_count") or 0) or 1.0
            no_ct = float(settle.get("no_count") or 0) or 1.0
            ticker_total_ct = yes_ct + no_ct
            ticker_fee_dollars = float(settle.get("fee_cents") or 0) / 100.0
            fee_share = (ticker_fee_dollars * ct / max(ticker_total_ct, 1))
            pnl = gross - fee_share

            d["pnl"] += pnl
            if pnl > 0:
                d["w"] += 1
                d["wins"].append(pnl)
            elif pnl < 0:
                d["l"] += 1
                d["losses"].append(pnl)

        # Manual fills (separate origin)
        try:
            c.execute(
                """
                SELECT count, side, action, price_cents, pnl_cents, settled
                FROM manual_fills
                WHERE created_at_ms > strftime('%s','now','start of day','-8 hours') * 1000
                """
            )
            man = {"n": 0, "w": 0, "l": 0, "pnl": 0.0, "wins": [],
                   "losses": [], "ct": 0, "unsettled": 0}
            for r in c.fetchall():
                man["n"] += 1
                man["ct"] += int(r["count"] or 0)
                if not int(r["settled"] or 0):
                    man["unsettled"] += 1
                    continue
                p = float(r["pnl_cents"] or 0) / 100.0
                man["pnl"] += p
                if p > 0:
                    man["w"] += 1
                    man["wins"].append(p)
                elif p < 0:
                    man["l"] += 1
                    man["losses"].append(p)
            out["MANUAL"] = man
        except sqlite3.OperationalError:
            pass  # manual_fills table might not exist on old DBs

        conn.close()
        # Compute derived metrics
        for tier, d in out.items():
            n = d.get("n", 0)
            d["wr"] = (d.get("w", 0) / n * 100.0) if n > 0 else 0.0
            d["avg_win"] = (sum(d["wins"]) / len(d["wins"])) if d.get("wins") else 0.0
            d["avg_loss"] = (sum(d["losses"]) / len(d["losses"])) if d.get("losses") else 0.0
    except Exception:
        pass
    return out


def _alpha_feature_status():
    """Inspect engine_history.log for the most-recent fire / activity from
    each alpha feature added 2026-04-28 (A: ARB, B: MICROPRICE, C: WALL,
    D: KALSHI POC, plus MANUAL TP, BOUNDED CLOSE, MAKER→TAKER ESCALATION).
    Returns dict mapping feature → (ts, last_event_str).
    """
    feats = {
        "ARB": {"pat": "ARB DETECTED", "ts": 0, "last": "(no fires)"},
        "MICRO": {"pat": "MICROPRICE-BID", "ts": 0, "last": "(no bumps)"},
        "WALL_LD": {"pat": "LATE_DOMINANT SIGNAL", "ts": 0, "last": "(no fires)"},
        "MAN_TP": {"pat": "MANUAL TP PLACED", "ts": 0, "last": "(no placements)"},
        "MAN_FILL": {"pat": "MANUAL FILL RECORDED", "ts": 0, "last": "(no captures)"},
        "BCLOSE": {"pat": "BOUNDED CLOSE", "ts": 0, "last": "(no sweeps)"},
        "TAKER": {"pat": "TAKER ESCALATE", "ts": 0, "last": "(no escalations)"},
        "OVERSELL": {"pat": "OVERSELL-DETECTED", "ts": 0, "last": "(none)"},
        "MAN_DET": {"pat": "MANUAL-DETECTED", "ts": 0, "last": "(none)"},
    }
    try:
        sz = os.path.getsize(LOG_PATH)
        # Read last ~2 MB so we cover at least the past few hours of activity
        # even on busy sessions (history log can be tens of MB).
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            f.seek(max(0, sz - 2_000_000))
            f.readline()  # skip first partial line after seek
            for line in f:
                for k, v in feats.items():
                    if v["pat"] in line:
                        # Parse timestamp prefix: "YYYY-MM-DD HH:MM:SS,mmm"
                        try:
                            ts_s = line[:19]
                            ts_dt = datetime.strptime(ts_s, "%Y-%m-%d %H:%M:%S")
                            ts_unix = ts_dt.replace(tzinfo=timezone.utc).timestamp()
                        except Exception:
                            ts_unix = 0
                        if ts_unix > v["ts"]:
                            v["ts"] = ts_unix
                            # Trim message to ~70 chars after the prefix
                            msg = line.split(":", 4)[-1].strip()[:80]
                            v["last"] = msg
    except Exception:
        pass
    return feats


def render_ops(data, log_lines, trades, stats, stale, age, width, live_bal):
    """OPS view: today's per-tier P&L + alpha feature status + recent
    manual+engine fills interleaved. Press 'o' to switch in.
    """
    W = max(80, min(width, 200))
    out = []
    bal_dollars = live_bal / 100.0 if live_bal else 0.0
    today = datetime.now().strftime("%Y-%m-%d")
    age_str = f"{age:.0f}s" if not stale else f"{RED}STALE {age:.0f}s{RESET}"

    # --- Header ---
    title = f" BTC BIAS ENGINE — OPS DASHBOARD  ({today}, age={age_str}, bal=${bal_dollars:.2f}) "
    out.append(_box_top(W))
    out.append(_box_line(f"{BOLD}{CYAN}{title}{RESET}", W))
    out.append(_box_mid(W))

    # --- Today's P&L by tier ---
    perf = _today_perf_breakdown()
    if perf:
        out.append(_box_line(f" {BOLD}TODAY (UTC day, engine + manual){RESET}", W))
        out.append(_box_line(
            f" {DIM}{'tier':<14} {'n':>3} {'ct':>5} {'wr':>6} {'avg_win':>8} {'avg_loss':>9} {'pnl':>10}{RESET}",
            W,
        ))
        total_pnl = 0.0
        for tier in ("TA_FORCED", "LATE_DOMINANT", "ARB", "SR_FADE",
                      "OTHER", "MANUAL"):
            d = perf.get(tier)
            if not d or d.get("n", 0) == 0:
                continue
            pnl = d.get("pnl", 0.0)
            total_pnl += pnl
            pnl_col = GREEN if pnl > 0 else RED if pnl < 0 else DIM
            unset = ""
            if tier == "MANUAL" and d.get("unsettled", 0) > 0:
                unset = f" {DIM}(+{d['unsettled']} unsettled){RESET}"
            line = (
                f" {tier:<14} "
                f"{d['n']:>3} {d.get('ct',0):>5} "
                f"{d.get('wr',0.0):>5.1f}% "
                f"{GREEN}{d.get('avg_win',0.0):>+8.2f}{RESET} "
                f"{RED}{d.get('avg_loss',0.0):>+9.2f}{RESET} "
                f"{pnl_col}{pnl:>+10.2f}{RESET}{unset}"
            )
            out.append(_box_line(line, W))
        out.append(_box_line(
            f" {BOLD}{'TOTAL':<14} {'':>3} {'':>5} {'':>6} {'':>8} {'':>9} "
            f"{(GREEN if total_pnl>0 else RED if total_pnl<0 else DIM)}{total_pnl:>+10.2f}{RESET}",
            W,
        ))
    else:
        out.append(_box_line(f" {DIM}No trades today yet{RESET}", W))
    out.append(_box_mid(W))

    # --- Alpha feature status (recent activity) ---
    feats = _alpha_feature_status()
    out.append(_box_line(f" {BOLD}ALPHA FEATURES — most recent activity{RESET}", W))
    now = time.time()
    feat_labels = [
        ("ARB",      "ARB detector (obs-only, no trades)"),
        ("TAKER",    "Maker→Taker escalation"),
        ("MICRO",    "Microprice limit-bid"),
        ("WALL_LD",  "LATE_DOMINANT (wall conf)"),
        ("BCLOSE",   "Bounded close sweep"),
        ("MAN_FILL", "Manual fills capture"),
        ("MAN_TP",   "Manual TP autoplacer"),
        ("MAN_DET",  "Manual-detection gate"),
        ("OVERSELL", "Oversell detection"),
    ]
    for key, label in feat_labels:
        v = feats.get(key, {})
        ts = v.get("ts", 0)
        if ts > 0:
            age_s = now - ts
            if age_s < 60:
                age_str_v = f"{GREEN}{age_s:.0f}s ago{RESET}"
            elif age_s < 3600:
                age_str_v = f"{YELLOW}{age_s/60:.0f}m ago{RESET}"
            else:
                age_str_v = f"{DIM}{age_s/3600:.1f}h ago{RESET}"
            last = v.get("last", "")[:W - 50]
            line = f" {label:<28} {age_str_v:<24} {DIM}{last}{RESET}"
        else:
            line = f" {label:<28} {DIM}{'(no activity yet)':<22}{RESET}"
        out.append(_box_line(line, W))
    out.append(_box_mid(W))

    # --- Daily-loss-limit usage bar ---
    daily_pnl = data.get("hourly", {}).get("daily_pnl", 0.0)
    daily_limit = 120.0  # mirrors user_config.DAILY_LOSS_LIMIT default
    used_frac = abs(min(0.0, daily_pnl)) / daily_limit
    bar = _cbar(used_frac, w=40, mx=1.0)
    pnl_col = GREEN if daily_pnl > 0 else RED if daily_pnl < 0 else DIM
    out.append(_box_line(
        f" {BOLD}DAY P&L:{RESET} {pnl_col}${daily_pnl:+.2f}{RESET}  "
        f"loss-limit usage {bar} {used_frac*100:.0f}% of -${daily_limit:.0f}",
        W,
    ))
    out.append(_box_mid(W))

    # --- Active position summary ---
    pos = data.get("position", {}) or {}
    if pos.get("open"):
        side = pos.get("side", "?")
        ct = pos.get("count", 0)
        entry = pos.get("entry_cents", 0)
        bid = pos.get("current_bid", 0)
        unreal = (bid - entry) * ct / 100.0 if entry > 0 else 0.0
        ucol = GREEN if unreal > 0 else RED if unreal < 0 else DIM
        ticker = pos.get("ticker", "")[-22:]
        out.append(_box_line(
            f" {BOLD}OPEN:{RESET} {_sc(side)} {ct}x @ {entry}c on {ticker[-22:]:<22}  "
            f"bid={bid}c  unreal={ucol}${unreal:+.2f}{RESET}",
            W,
        ))
    else:
        out.append(_box_line(f" {DIM}No open position{RESET}", W))
    out.append(_box_mid(W))

    # --- Recent fills feed (filtered log) ---
    out.append(_box_line(f" {BOLD}RECENT EVENTS (filtered){RESET}", W))
    interesting_pats = [
        "FILL:", "TP_FILLED", "ARB DETECTED", "ARB COMPLETE",
        "TAKER ESCALATE", "TAKER FILL", "MICROPRICE-BID",
        "LATE_DOMINANT SIGNAL", "LATE_DOMINANT MAKER", "LATE_DOMINANT STOP",
        "MANUAL FILL RECORDED", "MANUAL TP PLACED",
        "RESIDUAL-CLEAN", "OVERSELL", "BOUNDED CLOSE", "DAILY P&L",
    ]
    shown = 0
    for line in reversed(log_lines):
        if any(p in line for p in interesting_pats):
            # Trim timestamp prefix and the engine label
            try:
                # Format: "YYYY-MM-DD HH:MM:SS,mmm [LEVEL] xxx: msg"
                ts_part = line[:19]
                msg = line.split(": ", 1)[-1] if ": " in line else line
                msg = msg.replace("CopyEngine ", "")[:W - 24]
                col = GREEN if "FILL" in line and "+" in line else \
                      RED if "OVERSELL" in line else \
                      YELLOW if "ARB DETECTED" in line else \
                      WHITE
                out.append(_box_line(f" {DIM}{ts_part}{RESET} {col}{msg}{RESET}", W))
                shown += 1
                if shown >= 14:
                    break
            except Exception:
                continue
    if shown == 0:
        out.append(_box_line(f" {DIM}(no recent events matched){RESET}", W))
    out.append(_box_bot(W))

    out.append(f"  {DIM}[Tab] cycle  [d]ash [n]arr [b]oth [o]ps [h]elp [q]uit{RESET}")
    return "\n".join(out)


def render_help(width):
    """Help overlay — keybindings and view summaries. Press 'h' or '?' to toggle."""
    W = max(80, min(width, 100))
    out = []
    out.append(_box_top(W))
    out.append(_box_line(f"{BOLD}{CYAN} BTC BIAS ENGINE MONITOR — HELP {RESET}", W))
    out.append(_box_mid(W))
    out.append(_box_line(f" {BOLD}KEYS{RESET}", W))
    keys = [
        ("Tab", "cycle through views"),
        ("d", "DASHBOARD — main metrics + sparklines + log tail"),
        ("n", "NARRATIVE — natural-language status of the engine"),
        ("b", "BOTH — dashboard + narrative stacked"),
        ("o", "OPS — today's perf + alpha features + recent events"),
        ("h or ?", "this help screen"),
        ("r", "force refresh (clear screen)"),
        ("q", "quit"),
    ]
    for k, v in keys:
        out.append(_box_line(f"   {GREEN}{k:<8}{RESET}  {v}", W))
    out.append(_box_mid(W))

    out.append(_box_line(f" {BOLD}TIER GLOSSARY{RESET}", W))
    tiers = [
        ("TA_FORCED",     "FVG/Brownian-Bridge fair-value entries (LIVE)"),
        ("LATE_DOMINANT", "Final-3-min momentum-aligned entries (LIVE)"),
        ("ARB",           "Cross-side arb detector (OBSERVABILITY ONLY — "
                          "ARB_TRADES_ENABLED=False; data feeds FVG regime input)"),
        ("MANUAL",        "User manual trades captured via fills poller"),
        ("SR_FADE",       "S/R contract fade (DISABLED per 2026-04-27 directive)"),
    ]
    for k, v in tiers:
        out.append(_box_line(f"   {YELLOW}{k:<14}{RESET} {v}", W))
    out.append(_box_mid(W))

    out.append(_box_line(f" {BOLD}LOG MARKERS — what to watch for{RESET}", W))
    markers = [
        (f"{GREEN}FILL{RESET}",            "engine fill landed; check size + entry"),
        (f"{GREEN}TP_FILLED{RESET}",       "TP ladder partial — incremental win"),
        (f"{GREEN}MANUAL TP PLACED{RESET}","engine placed TP on your manual buy"),
        (f"{YELLOW}ARB DETECTED{RESET}",   "cross-side arb opportunity (logged only, no trade)"),
        (f"{YELLOW}ARB SESSION-CLASSIFIED{RESET}", "BILATERAL/DIRECTIONAL/DECIDED at window-open"),
        (f"{YELLOW}ARB TRADES-OFF{RESET}", "ARB detected real arb but trade gate is closed"),
        (f"{YELLOW}TAKER ESCALATE{RESET}", "maker stalled — paying spread to fill"),
        (f"{YELLOW}MICROPRICE-BID{RESET}", "depth-weighted entry refinement applied"),
        (f"{YELLOW}BOUNDED CLOSE{RESET}",  "post-TP sweep loop fired"),
        (f"{RED}OVERSELL-DETECTED{RESET}",  "engine sold more than held — investigate"),
        (f"{RED}STUCK-RESIDUAL{RESET}",     "manual cleanup needed on Kalshi UI"),
        (f"{CYAN}MANUAL FILL RECORDED{RESET}","your manual trade snapshotted to manual_fills"),
        (f"{CYAN}MANUAL-DETECTED{RESET}",    "safety reconciler left a manual position alone"),
    ]
    for k, v in markers:
        out.append(_box_line(f"   {k:<35} {v}", W))
    out.append(_box_bot(W))
    out.append(f"  {DIM}Press any key to return to your view.{RESET}")
    return "\n".join(out)


def _poll_key():
    """Non-blocking keypress check. Returns key string or None.

    Keys:
      Tab → cycle DASHBOARD/NARRATIVE/BOTH/OPS
      d   → DASHBOARD
      n   → NARRATIVE
      b   → BOTH
      o   → OPS (today's perf + alpha statuses + manual fills)
      h/? → HELP overlay
      r   → force refresh (clear screen)
      q   → quit
    """
    if sys.platform == "win32":
        try:
            import msvcrt
            if msvcrt.kbhit():
                ch = msvcrt.getch()
                if ch in (b"\t",):
                    return "TAB"
                if ch in (b"q", b"Q"):
                    return "Q"
                if ch in (b"n", b"N"):
                    return "NARR"
                if ch in (b"d", b"D"):
                    return "DASH"
                if ch in (b"b", b"B"):
                    return "BOTH"
                if ch in (b"o", b"O"):
                    return "OPS"
                if ch in (b"h", b"H", b"?"):
                    return "HELP"
                if ch in (b"r", b"R"):
                    return "REFRESH"
        except Exception:
            pass
    else:
        try:
            import select
            import termios
            import tty
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setcbreak(fd)
                if select.select([sys.stdin], [], [], 0)[0]:
                    ch = sys.stdin.read(1)
                    if ch == "\t":
                        return "TAB"
                    if ch in ("q", "Q"):
                        return "Q"
                    if ch in ("n", "N"):
                        return "NARR"
                    if ch in ("d", "D"):
                        return "DASH"
                    if ch in ("b", "B"):
                        return "BOTH"
                    if ch in ("o", "O"):
                        return "OPS"
                    if ch in ("h", "H", "?"):
                        return "HELP"
                    if ch in ("r", "R"):
                        return "REFRESH"
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except Exception:
            pass
    return None


def main():
    os.system("")
    if sys.platform == "win32":
        os.system("chcp 65001 >nul 2>&1")
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.write(HIDE_CURSOR + "\033[2J")
    sys.stdout.flush()

    state = DashboardState()
    log_tailer = LogTailer()
    trade_hist = TradeHistory()
    bal_fetcher = KalshiBalanceFetcher()
    bal_fetcher.start()

    # View modes: cycle order. OPS added 2026-04-28 (Claude per user) as a
    # primary perf-and-alpha-status dashboard; HELP is a transient overlay.
    view_modes = ["DASHBOARD", "NARRATIVE", "BOTH", "OPS"]
    view_idx = 0
    help_overlay = False
    last_key_t = 0.0
    quit_flag = False

    try:
        while not quit_flag:
            # Poll for key every loop iteration
            k = _poll_key()
            if k == "TAB":
                view_idx = (view_idx + 1) % len(view_modes)
                help_overlay = False
                sys.stdout.write("\033[2J")
                last_key_t = time.time()
            elif k == "DASH":
                view_idx = 0
                help_overlay = False
                sys.stdout.write("\033[2J")
            elif k == "NARR":
                view_idx = 1
                help_overlay = False
                sys.stdout.write("\033[2J")
            elif k == "BOTH":
                view_idx = 2
                help_overlay = False
                sys.stdout.write("\033[2J")
            elif k == "OPS":
                view_idx = 3
                help_overlay = False
                sys.stdout.write("\033[2J")
            elif k == "HELP":
                help_overlay = not help_overlay
                sys.stdout.write("\033[2J")
            elif k == "REFRESH":
                sys.stdout.write("\033[2J")
            elif k is not None and help_overlay:
                # Any other key while help is shown returns to the active view
                help_overlay = False
                sys.stdout.write("\033[2J")
            elif k == "Q":
                quit_flag = True
                break

            cols = shutil.get_terminal_size((80, 50)).columns

            if help_overlay:
                screen = render_help(cols)
                sys.stdout.write(HOME + screen + "\n")
                sys.stdout.flush()
                time.sleep(0.25)
                continue

            mode = view_modes[view_idx]
            data = state.refresh()

            if mode == "DASHBOARD":
                log_lines = log_tailer.tail(12)
                trades = trade_hist.refresh()
                screen = render(data, log_lines, trades, trade_hist.stats,
                                state.is_stale, state.age, cols, bal_fetcher.balance_cents,
                                state.pressure_history, trade_hist.bal_history)
                sys.stdout.write(HOME + screen + "\n")
            elif mode == "NARRATIVE":
                screen = render_narrative(data, state.is_stale, state.age, cols,
                                          bal_fetcher.balance_cents)
                sys.stdout.write(HOME + screen + "\n")
            elif mode == "OPS":
                log_lines = log_tailer.tail(60)  # bigger window for filtered events
                trades = trade_hist.refresh()
                screen = render_ops(data, log_lines, trades, trade_hist.stats,
                                     state.is_stale, state.age, cols,
                                     bal_fetcher.balance_cents)
                sys.stdout.write(HOME + screen + "\n")
            else:  # BOTH
                log_lines = log_tailer.tail(8)
                trades = trade_hist.refresh()
                dash = render(data, log_lines, trades, trade_hist.stats,
                              state.is_stale, state.age, cols, bal_fetcher.balance_cents,
                              state.pressure_history, trade_hist.bal_history)
                narr = render_narrative(data, state.is_stale, state.age, cols,
                                        bal_fetcher.balance_cents)
                sys.stdout.write(HOME + dash + "\n" + narr + "\n")
            sys.stdout.flush()
            time.sleep(0.25)  # faster loop so keys feel responsive
    except KeyboardInterrupt:
        pass
    finally:
        bal_fetcher.stop()
        sys.stdout.write(SHOW_CURSOR + RESET + "\n")
        sys.stdout.flush()

if __name__ == "__main__":
    main()
