"""Launch the PolymarketCopyEngine directly."""
import asyncio
import os
import sys
import logging

if getattr(sys, 'frozen', False):
    base = os.path.dirname(sys.executable)
else:
    base = os.path.dirname(os.path.abspath(__file__))
os.chdir(base)
if base not in sys.path:
    sys.path.insert(0, base)
os.makedirs(os.path.join(base, "data"), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(base, "data", "engine_history.log"), encoding="utf-8"),
    ],
)

logger = logging.getLogger(__name__)

async def main():
    logger.info("Main engine DISABLED — running cross-venue system only (Polymarket smart flow + Kalshi tape + whale monitor)")

    from kalshi_client import KalshiClient
    from signal_logger import SignalLogger
    from whale_monitor import WhaleMonitor

    # Load credentials — check env file, then prompt if missing
    cred_dir = os.path.join(base, "credentials")
    os.makedirs(cred_dir, exist_ok=True)
    env_file = os.path.join(cred_dir, "kalshi.env")

    # Try loading existing env file
    key_id = os.environ.get("KALSHI_API_KEY", "")
    pem_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip()
        key_id = os.environ.get("KALSHI_API_KEY", "")
        pem_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")

    # Interactive setup if credentials missing
    if not key_id or not pem_path or not os.path.exists(pem_path or ""):
        print("\n" + "=" * 60)
        print("  BTC Bias Engine — First-Time Setup")
        print("=" * 60)
        print("\nYou need a Kalshi API key and private key to trade.")
        print("Get these from: https://kalshi.com/account/settings/api\n")

        if not key_id:
            key_id = input("Kalshi API Key (UUID): ").strip()

        # Find the PEM/key file — accept .pem, .txt, or any text file
        if not pem_path or not os.path.exists(pem_path):
            print("\nPaste the FULL path to your private key file (.pem or .txt):")
            print("  Example: C:\\Users\\you\\Downloads\\kalshi_key.pem")
            pem_input = input("Key file path: ").strip().strip('"').strip("'")

            if os.path.exists(pem_input):
                # Copy key file into credentials/ for portability
                key_content = open(pem_input, "r").read()
                # Validate it looks like a PEM key
                if "PRIVATE KEY" in key_content or "BEGIN" in key_content:
                    local_pem = os.path.join(cred_dir, "kalshi_key.pem")
                    with open(local_pem, "w") as f:
                        f.write(key_content)
                    pem_path = local_pem
                    print(f"  Key copied to {local_pem}")
                else:
                    # Try reading as raw key content (user pasted a .txt with the key)
                    # Wrap in PEM headers if missing
                    if "-----" not in key_content:
                        print("  File doesn't look like a PEM key. Trying as raw key content...")
                        key_content = f"-----BEGIN RSA PRIVATE KEY-----\n{key_content.strip()}\n-----END RSA PRIVATE KEY-----\n"
                    local_pem = os.path.join(cred_dir, "kalshi_key.pem")
                    with open(local_pem, "w") as f:
                        f.write(key_content)
                    pem_path = local_pem
                    print(f"  Key saved to {local_pem}")
            else:
                print(f"  File not found: {pem_input}")
                return

        # Save env file for next launch
        if key_id and pem_path:
            with open(env_file, "w") as f:
                f.write(f"KALSHI_API_KEY={key_id}\n")
                f.write(f"KALSHI_PRIVATE_KEY_PATH={pem_path}\n")
                f.write("KALSHI_DEMO=false\n")
                f.write("EXECUTE_TRADES=true\n")
            print(f"\n  Credentials saved to {env_file}")
            print("  Next launch will use these automatically.\n")
            os.environ["KALSHI_API_KEY"] = key_id
            os.environ["KALSHI_PRIVATE_KEY_PATH"] = pem_path

    # Load the key
    pem = ""
    if pem_path and os.path.exists(pem_path):
        pem = open(pem_path, "r").read()
    if not pem or not key_id:
        logger.error("No Kalshi credentials found — run again and enter your API key + key file path")
        return

    signal_logger = SignalLogger()
    await signal_logger.initialize()

    copy_ref = {}
    whale_ref = {"state": None}

    # Start whale monitor
    whale = WhaleMonitor(whale_ref)
    whale_task = asyncio.create_task(whale.run())

    async with KalshiClient(key_id, pem) as client:
        from polymarket_copy_engine import PolymarketCopyEngine
        engine = PolymarketCopyEngine(
            kalshi_client=client,
            copy_engine_ref=copy_ref,
            signal_logger=signal_logger,
            whale_ref=whale_ref,
        )
        await engine.run()

if __name__ == "__main__":
    asyncio.run(main())
