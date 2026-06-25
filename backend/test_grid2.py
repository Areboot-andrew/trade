import json
import asyncio
from server import internal_calculate_grid, STATE

async def test():
    STATE["bot_config"]["initial_margin"] = "10"
    STATE["bot_config"]["leverage"] = "40"
    STATE["bot_config"]["margin_multiplier"] = "1.2"
    STATE["bot_config"]["cluster_count"] = "3"
    STATE["symbol"] = "SOLUSDT"
    res = await internal_calculate_grid()
    print(json.dumps(res["long_grid"][:3], indent=2))

if __name__ == "__main__":
    asyncio.run(test())
