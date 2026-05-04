"""Quick live state check for monitoring loops."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kalshi_client import KalshiClient


async def main() -> None:
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_path = os.path.join(base, "credentials", "kalshi.env")
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip()
    pem = open(os.environ["KALSHI_PRIVATE_KEY_PATH"]).read()
    async with KalshiClient(os.environ["KALSHI_API_KEY"], pem) as c:
        bal = await c.get_balance()
        print(f"BAL: ${bal.balance/100:.2f}")
        positions = await c.get_positions()
        non_zero = [p for p in (positions or []) if int(float(p.get("position_fp", 0) or 0)) != 0]
        print(f"Position: {'FLAT' if not non_zero else 'NOT FLAT'}")
        for p in non_zero:
            qty = int(float(p.get("position_fp", 0) or 0))
            print(f"  {p.get('ticker')} qty={qty}")
        data = await c._request("GET", "/portfolio/orders", params={"status": "resting", "limit": 50})
        orders = data.get("orders", [])
        print(f"Resting: {len(orders)}")
        for o in orders:
            print(f"  {o.get('ticker')} {o.get('side')} {o.get('action')} {o.get('count')}@{o.get('yes_price') or o.get('no_price')}c")


if __name__ == "__main__":
    asyncio.run(main())
