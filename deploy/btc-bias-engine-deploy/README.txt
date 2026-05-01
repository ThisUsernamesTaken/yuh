BTC Bias Engine -- Kalshi KXBTC15M Binary Contract Trader
==========================================================

One-time setup (5 minutes):

1. Get Kalshi API credentials
   - https://kalshi.com/account/settings/api
   - Create an API key and copy the UUID
   - Download the RSA private key (.pem file)

2. Run setup
   - Double-click:  Setup.bat
   - Paste your UUID when prompted
   - Paste the full path to your .pem file
   - It writes credentials\kalshi.env and copies the PEM next to it

Running the system (two windows):

   Window 1 - TRADING ENGINE
       Double-click:  Run_Engine.bat
       Trades live. Logs to data\engine_history.log.

   Window 2 - LIVE MONITOR (optional, read-only)
       Double-click:  Run_Monitor.bat
       Real-time dashboard: pressure, position, P&L, session timer.
       Reads data\dashboard_state.json -- safe to close/reopen anytime.

Strategy:
- Trades Kalshi KXBTC15M contracts (15-min BTC up/down binary)
- Microstructure pressure engine (BTC impulse, book pressure, taker flow, Kalshi lag)
- Brownian Bridge probability model for fair value + TP placement
- FVG baseline divergence filter
- Dynamic trailing exits, sweep-sell on partial fills, DCA tiers

Files:
- BTC_Engine.exe           Main executable (engine + monitor in one)
- Setup.bat                First-run credential wizard
- Run_Engine.bat           Start the trading engine
- Run_Monitor.bat          Open the live terminal monitor
- credentials\             Your API key + PEM (never shared, never committed)
- data\                    Trade history, logs, dashboard state

Tuning:
- Edit user_config.py (created in the app folder on first run) to change:
     SIZING_BALANCE_FRACTION   (fraction of balance per trade)
     SIZING_MAX_DOLLARS        (hard cap per position)
     DAILY_LOSS_LIMIT          (auto-halt threshold)
     MAX_TRADES_PER_WINDOW     (entries per 15-min window; recommended: 1)

Stopping:
- Close the engine window, or Ctrl+C in it.
- Closing the monitor does NOT stop trading.

Security:
- NO secrets ship with this package. The EXE is credential-free.
- Your kalshi.env and .pem live only in the credentials\ folder on your PC.
- Never share the credentials folder.

Command-line flags (advanced):
   BTC_Engine.exe              Interactive menu
   BTC_Engine.exe --engine     Headless trading loop
   BTC_Engine.exe --monitor    Live terminal dashboard
   BTC_Engine.exe --setup      Re-run credential wizard
