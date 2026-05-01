# RESEARCH — Remote Terminal Viewer Options — Do Not Implement

**Date:** 2026-04-12  
**Scope:** Read-only research. No code changes.

---

## 1. Terminal UI File Review

### File: `btc-bias-engine/app.pyw` (1,244 lines)

**Framework:** Pure `tkinter` + `ttk`. No web server, no REST API, no WebSocket. Strictly a local GUI window.

**Window geometry:** 1080×820 px, resizable. Dark theme (GitHub-dark palette: `#0d1117` background).

**Data source — how it connects to the engine:**  
The dashboard does **not** import the engine or share memory with it. It polls `data/engine_history.log` every **1 second** via a background thread, then uses ~60 regex patterns to parse structured log lines into state variables. It also detects if the engine is running as an NSSM Windows service via `nssm status BTCBiasEngine`.

A secondary data artifact exists: **`data/dashboard_state.json`** — the engine writes this file each cycle with fully structured JSON (see §1.5 below). `app.pyw` does not currently read this file — it relies solely on the log — but this JSON is the cleanest data source for any remote viewer.

**Refresh rate:** 1-second poll loop (`self.after(1000, self._poll)`). Log tail reads ~50 pending lines per tick, UI redraws on every poll.

**Layout — six panels top-to-bottom:**

| Panel | Contents |
|-------|----------|
| **Header** | "BTC BIAS ENGINE" title, animated status dot (GREEN=LIVE / RED=STOPPED), Stop/Restart/Settings buttons |
| **Big numbers** | Account balance ($), today P&L (+/-), W/L record + win-rate %, filtered-signals count, current position (side + size + entry price), window time range |
| **Charts** | Two side-by-side 420×120 canvas charts: contract mid-price (last 90 ticks, entry line, TP lines) + cumulative P&L curve |
| **Algorithm** | Direction label (YES/NO/DEAD/~~), conviction score (pts + reason), sizing %, animated signal bars (Lean/TA/MTF/Flow/Mom), status badges (BURST, LADDER, MTF, WHALE, FLIP, SKIP) |
| **Prob Engine + FVG + Regime** | Three sub-columns: Regime (TRENDING/MEAN_REVERTING/EXPLOSIVE, vol%, trend score), Prob Engine (fair value cents, probability %, edge cents, vol%), FVG Baseline (baseline cents, FVG gap, BTC distance %, status: IDLE/ARMED/ENTERED) |
| **Live feed** | Scrolling color-coded log text (300-line rolling window): entries, fills, exits, whale alerts, flow events, tier fills |

**Key state variables tracked in memory:**  
`_balance`, `_pnl`, `_cum_pnl`, `_wins`, `_losses`, `_pos`, `_mid`, `_ta_conf`, `_ta_rsi`, `_mtf_score`, `_mtf_regime`, `_conv`, `_conv_r`, `_flow_p`, `_flow_d`, `_regime`, `_regime_vol`, `_prob_fair`, `_prob_pct`, `_prob_edge`, `_prob_vol`, `_fvg_baseline`, `_fvg_gap`, `_fvg_status`, `_btc_dist`, `_burst`, `_ladder`, `_whale`, `_window`

### 1.5 `data/dashboard_state.json` — The Hidden Gem

The engine already writes a structured JSON file each cycle. Current schema (sampled live):

```json
{
  "ts": 1776027096.96,
  "session": { "ticker": "KXBTC15M-...", "seconds_remaining": 499.1, "regime": "EXPLOSIVE", "regime_vol": 46.3, "window_locked": false, "trades_this_window": 0 },
  "prob": { "fair_value": 45.7, "probability": 0.4572, "mispricing": 17.7, "volatility": 50.9, "edge_cents": 17.7, "side": "yes" },
  "fvg": { "fvg_vs_baseline": 5.7, "crossed": false, "btc_dist_pct": 0.0218, "entry_ready": false },
  "tape": { "mid_cents": 28, "taker_imbalance": 0.0, "vwap_cents": 50.0 },
  "btc": { "price": 71247.58, "session_open": 71377.06, "distance_pct": 0.0218 },
  "ta": { "direction": "up", "confidence": 31, "rsi": 53.6, "adx": 40.3 },
  "five_sec": { "bb_upper": 71315.58, "bb_mid": 71284.15, "bb_lower": 71252.73, "tick_velocity": -0.01 },
  "position": { "active": false },
  "account": { "balance_cents": 5313, "daily_pnl": -2.17 },
  "flow": { "direction": "", "conviction": 0.0, "up_wallets": 0, "down_wallets": 0 },
  "pressure": { "score": 0.0262, "crossed": false, "ready": false }
}
```

This covers every panel in `app.pyw` except the scrolling live-feed log. A remote viewer reading this file (or served from it) would have nearly full fidelity.

---

## 2. Remote Viewing Options

### Option A — Android App (Native)

**Architecture:**  
Engine → writes `dashboard_state.json` → lightweight Python REST server (FastAPI/Flask, ~50 lines) reads the file and serves `/state` → Cloudflare Tunnel (or ngrok) exposes port → Android app polls `/state` every 2-5 seconds.

**What gets exposed:**  
Balance, position, signals, regime, prob engine fair value, P&L. Everything except the scrolling log feed (that would need a separate `/logs` endpoint returning the last N lines from `engine_history.log`).

**Effort:** Medium. Requires writing a small Android app (Kotlin/Jetpack Compose or React Native), the REST server, and securing the tunnel endpoint.

**Security:**  
- Trading API keys are never in the state JSON (engine reads them from env/cred file, not written out)  
- Still: balance and position data is sensitive. Must use a shared secret header or basic auth on the tunnel  
- The phone never touches Kalshi — it's read-only dashboard data  
- Risk: if the tunnel is misconfigured, anyone with the URL can see your trading state

**Android API 26 (Oreo) support:** Full REST polling via OkHttp or Retrofit. Background refresh via WorkManager (API 23+). Push-style notifications on new fills via FCM (if added). No compatibility issues.

**Verdict:** High value but highest effort. Worthwhile only if you want a polished persistent app.

---

### Option B — Web-Based Live Terminal Viewer

**Architecture:**  
Engine → writes `dashboard_state.json` + `engine_history.log` → FastAPI server with WebSocket push → Cloudflare Tunnel → browser on phone (Chrome/Firefox).

**Two sub-approaches:**

**B1 — Custom web dashboard (best fidelity):**  
Replicate the `app.pyw` layout as HTML/CSS/JS. Server sends JSON state via WebSocket every 1-2s. The page renders animated bars, color-coded log feed, P&L chart (Chart.js), etc. Can be saved as a PWA on Android home screen.

**B2 — Raw log streaming (minimal effort):**  
Use [ttyd](https://github.com/tsl0922/ttyd) or [GoTTY](https://github.com/yudai/gotty) to serve a terminal that `tail -f`s the log file. No UI fidelity — just scrolling text. Very fast to set up but hard to read on mobile.

**Effort:**  
- B1: Medium-high (build a web dashboard). The `dashboard_state.json` schema does most of the work — the JSON keys map directly to `app.pyw`'s display labels.  
- B2: Low (10 minutes). Install ttyd, point at `tail -f data/engine_history.log`, expose via Cloudflare Tunnel.

**Security:** Same as Option A. Cloudflare Tunnel with a secret path or basic auth.

**Android considerations:** PWA can be pinned to home screen, gets near-native UX. Background refresh not possible in mobile browser (tab must be open), but can use push notifications via Web Push API if the server sends them.

---

### Option C — Cloud Relay (Engine Pushes to Cloud)

**Architecture:**  
Engine → pushes `dashboard_state.json` to cloud (Supabase real-time, Firebase Realtime DB, or simple Cloudflare R2/S3 PUT every 5s) → Android app or mobile browser reads from cloud URL.

**No inbound connections to the trading machine.** The engine only makes outbound HTTPS requests — firewall-friendly, works even behind CGNAT.

**Implementation paths:**
- **Cloudflare R2 + Workers:** Engine POSTs state JSON to a Workers endpoint every 5s. Worker writes to R2 or KV. Mobile browser fetches from a public (or secret-key-gated) URL. Free tier covers this volume.
- **Supabase:** Engine UPSERTs a row into a `dashboard_state` table. Mobile app uses Supabase JS SDK or REST. Real-time subscriptions possible.
- **Firebase RTDB:** Engine writes via REST API. Mobile browser uses Firebase SDK for live updates.

**Effort:** Low-medium. The engine already writes `dashboard_state.json` — adding an outbound POST call is 10-20 lines of Python in the engine's main loop.

**Security:**  
- Lowest risk: the trading PC accepts zero inbound connections  
- Use a secret API key in the POST header to authenticate writes  
- The cloud endpoint is read-only to the outside world  
- Balance and position data still lives in the cloud — use a secret path or gated read

**Reliability:** Depends on cloud uptime. A temporary network hiccup means the mobile view goes stale, but the engine is unaffected.

**Verdict:** Best security posture, lowest complexity. No Android app needed — works in any mobile browser.

---

### Option D — Existing Tools (Zero Code)

#### D1 — Chrome Remote Desktop
- **How:** Install Chrome Remote Desktop on the trading PC. Access via `remotedesktop.google.com` or the Android app.
- **Shows:** Full desktop, including the `app.pyw` tkinter window exactly as it looks on screen.
- **Pros:** Zero code, full fidelity, works now.
- **Cons:** Requires Google account on trading PC. Performance depends on network — latency of 100-300ms is typical. The whole 1080×820 window is tiny on a phone; requires pinch-zoom. Background reconnects on Android can be slow. Google has access to your screen.
- **Security:** Authenticated by Google account. No trading credentials exposed beyond what's visible on screen.

#### D2 — RustDesk (self-hosted alternative)
- **How:** Install RustDesk on trading PC and phone. Can self-host the relay server or use their public relay.
- **Shows:** Full desktop, same as Chrome Remote Desktop.
- **Pros:** Open source, no Google dependency. Self-hosted relay = no third-party sees your screen.
- **Cons:** More setup than Chrome Remote Desktop. Public relay has same privacy concern as Chrome RD.

#### D3 — Tailscale + SSH
- **Limitation:** Does NOT help here. `app.pyw` is a GUI window (tkinter), not a terminal process. SSH gives you a shell, not a window. The engine's output goes to `engine_history.log`, not stdout (it runs under NSSM with `PYTHONUNBUFFERED=1` and stdout redirected to the log file). You could `tail -f` the log over SSH, but that's raw text, not the dashboard UI.
- **Use case:** Useful for remote debugging/config changes, not for viewing the dashboard.

#### D4 — Tailscale + Options B or C
- Tailscale creates a mesh VPN between the trading PC and your phone with no public inbound ports.
- Combined with Option B (web dashboard), the phone accesses the FastAPI server at a private Tailscale IP. No cloud relay, no public tunnel, no Cloudflare needed.
- **Best security posture for a local web dashboard.**

#### D5 — Parsec
- Designed for game streaming. Very low latency (20-50ms). Shows full desktop.
- Overkill for a trading dashboard. Parsec's servers intermediate the stream.

---

## 3. Recommended Approach

**Short-term (today, zero effort):** Option D1 — Chrome Remote Desktop. Install it, done. The tkinter window is visible on your phone immediately. Zoom into the balance/position section when needed.

**Medium-term (best balance of effort + capability):** Option C + Tailscale.

1. Add ~20 lines to the engine's main loop to POST `dashboard_state.json` to a Cloudflare Worker or Supabase endpoint every 5 seconds.
2. Build a single-page HTML dashboard (or use a no-code tool like Retool/Glide) that reads the endpoint.
3. Or skip the cloud entirely: use Tailscale to VPN the phone into the trading PC's network, then serve the JSON from a minimal FastAPI server on localhost — no public internet exposure at all.

This approach:
- Requires no new Android app code
- The `dashboard_state.json` already has all data needed
- The engine makes only outbound connections (or stays local via Tailscale)
- Works in any mobile browser
- Can be a PWA pinned to Android home screen

---

## 4. Implementation Sketch (Recommended: Tailscale + Local Web Server)

> **RESEARCH ONLY — sketch only, not a build plan**

**Step 1 — Install Tailscale on trading PC and Android phone**  
Both appear on the same mesh VPN. Trading PC gets a stable `100.x.x.x` address.

**Step 2 — Minimal FastAPI server (new file, ~60 lines)**
```python
# serve_dashboard.py  (DO NOT IMPLEMENT — sketch only)
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import json, pathlib

app = FastAPI()
STATE = pathlib.Path("data/dashboard_state.json")
LOG   = pathlib.Path("data/engine_history.log")

@app.get("/state")
def state():
    return JSONResponse(json.loads(STATE.read_text()))

@app.get("/logs")
def logs():
    lines = LOG.read_text(errors="ignore").splitlines()[-100:]
    return {"lines": lines}

# uvicorn serve_dashboard:app --host 0.0.0.0 --port 8765
```

**Step 3 — Single-page HTML dashboard**  
JavaScript polls `/state` every 2 seconds. Renders balance, position, prob engine panel, regime. A second fetch to `/logs` renders the live feed. No build tools needed — plain HTML file served as a static file from the same FastAPI server.

**Step 4 — Access on Android**  
Open `http://100.x.x.x:8765` in Chrome. Add to home screen as PWA. The Tailscale VPN keeps it private — only your devices can reach the address.

**Step 5 (optional) — Push notifications on fills**  
Server-sent events (SSE) stream from `/events` endpoint. JavaScript `EventSource` keeps a persistent connection. When `dashboard_state.json` shows a new fill or position change, the page triggers a browser notification.

---

## 5. Android-Specific Considerations

**API 26 (Oreo) capabilities relevant here:**
- **Background execution limits (API 26+):** Background services are restricted. If the Android app polls via WorkManager, periodic intervals are minimum 15 minutes (JobScheduler constraint). For real-time monitoring, the page/app must be in the foreground.
- **Notification channels (API 26, mandatory):** Any local notifications require a `NotificationChannel`. Oreo introduced channel categories (HIGH_PRIORITY for fill alerts). A PWA using the Web Notifications API can trigger these from the browser without a native app.
- **WebSocket / SSE in Chrome (Android):** Fully supported. SSE is simpler than WebSocket for one-way server→client push.
- **PWA home screen install:** Chrome on Android API 26+ supports "Add to Home Screen" as a proper PWA with custom icon. The browser wraps the page in a standalone window (no address bar). Works well for a trading dashboard.
- **Screen stay-awake:** A PWA can request `navigator.wakeLock.request('screen')` (API available in Chrome 84+). Android 8 supports Chrome 68+, so this is borderline — a newer Chrome version may be needed. Alternative: set screen timeout manually while monitoring.
- **Battery and background:** If the tab is backgrounded, Chrome on Android may throttle timers to once per minute. For live monitoring, the screen needs to stay on and the PWA in the foreground. A phone on a charger mount works well for this use case.
- **Termux (SSH client):** Termux on Android supports full SSH. Combined with Tailscale, you can `tail -f data/engine_history.log` over SSH. Useful for debugging but not for dashboard viewing.

---

*RESEARCH ONLY — Remote Terminal Viewer Options — Do Not Implement*
