"""Analyze P&L by hour from engine_history.log"""
import re
from collections import defaultdict

trades = []
with open(r"C:\Trading\btc-bias-engine\data\engine_history.log", "r", errors="ignore") as f:
    for line in f:
        # Wins: sell ladder complete
        m = re.search(r"(\d{4}-\d{2}-\d{2} (\d{2}):\d{2}:\d{2}).*SELL LADDER: \d+x sold.*\+\$([0-9.]+) \| 0x remaining", line)
        if m:
            trades.append(("win", int(m.group(2)), float(m.group(3))))
            continue
        # Wins: priced-in exit
        m = re.search(r"(\d{4}-\d{2}-\d{2} (\d{2}):\d{2}:\d{2}).*PRICED-IN EXIT.*\$\+([0-9.]+)", line)
        if m:
            trades.append(("win", int(m.group(2)), float(m.group(3))))
            continue
        # Wins: profit lock
        m = re.search(r"(\d{4}-\d{2}-\d{2} (\d{2}):\d{2}:\d{2}).*PROFIT LOCK.*\$\+([0-9.]+)", line)
        if m:
            trades.append(("win", int(m.group(2)), float(m.group(3))))
            continue
        # Losses: extreme exit
        m = re.search(r"(\d{4}-\d{2}-\d{2} (\d{2}):\d{2}:\d{2}).*EXTREME.*EXIT.*\$-([0-9.]+)", line)
        if m:
            trades.append(("loss", int(m.group(2)), -float(m.group(3))))
            continue
        # Losses: no-bounce
        m = re.search(r"(\d{4}-\d{2}-\d{2} (\d{2}):\d{2}:\d{2}).*NO-BOUNCE EXIT.*\$-([0-9.]+)", line)
        if m:
            trades.append(("loss", int(m.group(2)), -float(m.group(3))))
            continue
        # Losses: expiry
        m = re.search(r"(\d{4}-\d{2}-\d{2} (\d{2}):\d{2}:\d{2}).*EXPIRY.*-\$([0-9.]+)", line)
        if m:
            trades.append(("loss", int(m.group(2)), -float(m.group(3))))
            continue

hours = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0, "trades": 0})
for typ, hour, pnl in trades:
    hours[hour]["trades"] += 1
    hours[hour]["pnl"] += pnl
    if typ == "win":
        hours[hour]["wins"] += 1
    else:
        hours[hour]["losses"] += 1

print("HOUR(UTC) HOUR(ET)  TRADES  W    L    P&L       WR")
print("-" * 60)
for h in range(24):
    d = hours[h]
    if d["trades"] == 0:
        continue
    et = (h - 4) % 24
    wr = d["wins"] / d["trades"] * 100 if d["trades"] else 0
    flag = " ***" if d["pnl"] < -3 else " +++" if d["pnl"] > 3 else ""
    print(f"  {h:02d} UTC    {et:02d} ET    {d['trades']:3d}   {d['wins']:2d}   {d['losses']:2d}   ${d['pnl']:+7.2f}   {wr:4.0f}%{flag}")

print(f"\nTotal: {len(trades)} trades, ${sum(t[2] for t in trades):+.2f}")

print("\n4-HOUR BLOCKS:")
for start_utc, label in [(4, "12-4AM ET"), (8, "4-8AM ET"), (12, "8AM-12PM ET"), (16, "12-4PM ET"), (20, "4-8PM ET"), (0, "8PM-12AM ET")]:
    bp = sum(hours[(start_utc + i) % 24]["pnl"] for i in range(4))
    bt = sum(hours[(start_utc + i) % 24]["trades"] for i in range(4))
    bw = sum(hours[(start_utc + i) % 24]["wins"] for i in range(4))
    if bt == 0:
        continue
    wr = bw / bt * 100
    flag = " *** KILL ZONE" if bp < -5 else " +++ PROFIT ZONE" if bp > 5 else ""
    print(f"  {label:15s} {bt:3d} trades  {bw}W  ${bp:+7.2f}  {wr:.0f}% WR{flag}")
