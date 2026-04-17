"""
Standalone paper trading engine runner.
Replicates server.py initialization then starts the engine in simulation mode.
"""
import asyncio
import logging
import os

# Must run from the project root so .env is found
os.chdir(os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


async def main():
    from polymarket_mcp.config import load_config
    from polymarket_mcp.auth import create_polymarket_client
    from polymarket_mcp.tools import engine_tools

    # 1. Load config (.env)
    logger.info("Loading config...")
    config = load_config()
    logger.info(f"Address: {config.POLYGON_ADDRESS or '(demo)'} | DEMO_MODE={config.DEMO_MODE}")

    # 2. Init client (read-only ok for simulation)
    client = create_polymarket_client(
        private_key=config.POLYGON_PRIVATE_KEY,
        address=config.POLYGON_ADDRESS,
        chain_id=config.POLYMARKET_CHAIN_ID,
        api_key=config.POLYMARKET_API_KEY,
        api_secret=config.POLYMARKET_PASSPHRASE,
        passphrase=config.POLYMARKET_PASSPHRASE,
    )

    # Try to get API creds (non-fatal)
    if not client.has_api_credentials():
        try:
            await client.create_api_credentials()
            logger.info("API credentials created")
        except Exception as e:
            logger.warning(f"No API creds (read-only): {e}")

    # 3. Wire client into engine_tools
    engine_tools.set_client(client)

    # 4. Start engine in paper mode
    logger.info("Starting engine in SIMULATION (paper) mode...")
    result = await engine_tools.handle_tool_call("start_engine", {
        "simulation": True,
        "budget_per_slot": 20.0,
        "strategy": "orderbook_spread",
        "enable_price_feeds": True,
    })
    for r in result:
        logger.info(r.text)

    # 5. Poll status every 30s indefinitely
    while True:
        await asyncio.sleep(30)
        status = engine_tools._engine_status()
        for r in status:
            logger.info("\n" + r.text)


if __name__ == "__main__":
    asyncio.run(main())
