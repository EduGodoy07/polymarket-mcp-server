"""
Ichimoku live trading runner — paper mode (DEMO_MODE=true).

Usa IchimokuStrategy (5 componentes, fix H/L, Chikou Span).
Corre en modo simulación: nunca ejecuta órdenes reales.

Uso:
    venv/bin/python run_ichimoku_live.py

Ctrl+C para detener. El estado se imprime cada 60 segundos.
"""
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "src")

# Cargar .env antes de cualquier import del proyecto
from dotenv import load_dotenv
load_dotenv(".env")

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
# Silenciar librerías ruidosas, mantener nuestros logs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("polymarket_mcp.engine.backtester").setLevel(logging.WARNING)

log = logging.getLogger("ichimoku_live")


# ── Helpers de display ────────────────────────────────────────────────────

def fmt_pnl(val: float) -> str:
    return f"\033[92m+${val:.2f}\033[0m" if val >= 0 else f"\033[91m-${abs(val):.2f}\033[0m"

def print_status(status: dict, completed: int) -> None:
    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    pnl = status.get("total_pnl_usd", 0.0)
    bal = status.get("paper_balance", 500.0)
    active = len(status.get("active_slots", []))

    print(f"\n{'─'*55}")
    print(f"  🕐 {now}  |  Ichimoku Live (Paper)")
    print(f"{'─'*55}")
    print(f"  Estado       : {status.get('state', '?')}")
    print(f"  Balance paper: ${bal:.2f}")
    print(f"  PnL total    : {fmt_pnl(pnl)}")
    print(f"  Slots activos: {active}")
    print(f"  Slots compl. : {completed}")
    if active:
        for s in status.get("active_slots", []):
            print(f"    ↳ {s.get('market_id','?')[:20]}… "
                  f"state={s.get('state','?')}")
    print(f"{'─'*55}\n")


# ── Main ──────────────────────────────────────────────────────────────────

async def main() -> None:
    from polymarket_mcp.config import load_config
    from polymarket_mcp.auth import create_polymarket_client
    from polymarket_mcp.engine.engine import TradingEngine
    from polymarket_mcp.engine.strategies import IchimokuStrategy

    # 1. Config
    log.info("Cargando configuración...")
    config = load_config()
    log.info(f"  Address  : {config.POLYGON_ADDRESS or '(demo)'}")
    log.info(f"  Demo mode: {config.DEMO_MODE}")

    if not config.DEMO_MODE:
        log.error("¡DEMO_MODE=false detectado! Configura DEMO_MODE=true en .env")
        sys.exit(1)

    # 2. Cliente (solo lectura está bien para simulation)
    client = create_polymarket_client(
        private_key=config.POLYGON_PRIVATE_KEY,
        address=config.POLYGON_ADDRESS,
        chain_id=config.POLYMARKET_CHAIN_ID,
        api_key=config.POLYMARKET_API_KEY,
        api_secret=config.POLYMARKET_PASSPHRASE,
        passphrase=config.POLYMARKET_PASSPHRASE,
    )
    if not client.has_api_credentials():
        try:
            await client.create_api_credentials()
        except Exception as e:
            log.warning(f"Sin credenciales API (modo lectura): {e}")

    # 3. Engine con IchimokuStrategy
    engine = TradingEngine(
        client=client,
        strategy_class=IchimokuStrategy,
        simulation=True,           # SIEMPRE paper en este runner
        budget_per_slot=20.0,      # $20 por slot (ajusta según capital)
        max_concurrent_slots=1,    # 1 slot a la vez
    )

    log.info("─" * 55)
    log.info("  Ichimoku Live Runner — PAPER TRADING")
    log.info(f"  Estrategia : {IchimokuStrategy.name}")
    log.info(f"  Budget/slot: $20.00")
    log.info(f"  Simulation : True (nunca ordenes reales)")
    log.info("─" * 55)

    # 4. Arrancar
    await engine.start()
    log.info("Motor arrancado. Buscando slots BTC cada 30s...")
    log.info("Ctrl+C para detener\n")

    # 5. Loop de status
    try:
        while True:
            await asyncio.sleep(60)
            status = engine.get_status()
            print_status(status, len(engine._completed_slots))

            # Mostrar últimos 3 trades completados
            history = engine.get_pnl_history()
            if history:
                print("  Últimos trades:")
                for h in history[-3:]:
                    side   = h.get("side", "?")
                    pnl_v  = h.get("pnl", 0.0)
                    market = h.get("market_id", "?")[:18]
                    print(f"    {market}… {side:3s}  {fmt_pnl(pnl_v)}")
                print()

    except KeyboardInterrupt:
        log.info("\nDeteniendo engine...")
        await engine.stop()

        # Resumen final
        status = engine.get_status()
        history = engine.get_pnl_history()
        total_pnl = sum(h.get("pnl", 0) for h in history)
        wins  = sum(1 for h in history if h.get("pnl", 0) > 0)
        total = len(history)

        print("\n" + "═" * 55)
        print("  RESUMEN SESIÓN — Ichimoku Paper Trading")
        print("═" * 55)
        print(f"  Slots operados : {total}")
        print(f"  Win rate       : {100*wins/total:.1f}%" if total else "  Sin trades")
        print(f"  PnL total      : {fmt_pnl(total_pnl)}")
        print(f"  Balance final  : ${status.get('paper_balance', 500):.2f}")
        print("═" * 55)
        log.info("Runner detenido.")


if __name__ == "__main__":
    asyncio.run(main())
