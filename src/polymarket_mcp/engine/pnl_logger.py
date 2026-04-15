"""
PnL Logger and Chart Visualizer for the trading engine.

Structured per-slot logging → JSONL file for each session.
Chart generator → self-contained interactive HTML using Plotly CDN,
one chart per market window showing: price timeline, buy/sell events,
stop-loss line, take-profit line, cumulative PnL.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .lifecycle import SlotPnL
from .strategy import OrderReceipt, OrderResult

logger = logging.getLogger(__name__)

DEFAULT_LOG_DIR = Path.home() / ".polymarket_engine" / "logs"


class PnLLogger:
    """
    Records slot results, trade receipts, and risk events to disk.
    Generates interactive HTML charts from the recorded data.
    """

    def __init__(self, log_dir: Optional[Path] = None, strategy_name: str = "unknown"):
        self.log_dir = Path(log_dir) if log_dir else DEFAULT_LOG_DIR
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.strategy_name = strategy_name

        session_ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self._session_file = self.log_dir / f"session_{strategy_name}_{session_ts}.jsonl"
        self._slots: List[Dict[str, Any]] = []

        logger.info(f"PnLLogger initialized → {self._session_file}")

    # ── Recording ─────────────────────────────────────────────────────────

    def record(self, pnl: SlotPnL, receipts: List[OrderReceipt]) -> None:
        """Persist one completed slot's data."""
        entry = {
            "type": "slot",
            "pnl": pnl.to_dict(),
            "trades": [self._receipt_to_dict(r) for r in receipts],
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        self._slots.append(entry)
        self._append_jsonl(entry)
        logger.info(
            f"[LOG] {pnl.market_id[:12]} PnL={pnl.total_pnl:+.4f} "
            f"trades={len(receipts)}"
        )

    def record_risk_event(self, event) -> None:
        entry = {"type": "risk_event", **event.to_dict()}
        self._append_jsonl(entry)

    def record_price_tick(
        self,
        market_id: str,
        price: float,
        sources: Optional[Dict[str, float]] = None,
    ) -> None:
        """Optional: record price ticks for richer charts."""
        entry = {
            "type": "price",
            "market_id": market_id,
            "price": price,
            "sources": sources or {},
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        self._append_jsonl(entry)

    # ── Summary ───────────────────────────────────────────────────────────

    def session_summary(self) -> Dict[str, Any]:
        slots = [e for e in self._slots if e["type"] == "slot"]
        if not slots:
            return {"slots": 0, "total_pnl": 0.0}

        pnls = [s["pnl"]["total_pnl"] for s in slots]
        wins = sum(1 for p in pnls if p > 0)
        losses = sum(1 for p in pnls if p < 0)

        return {
            "strategy": self.strategy_name,
            "slots": len(slots),
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / len(slots) * 100, 1) if slots else 0,
            "total_pnl": round(sum(pnls), 4),
            "avg_pnl": round(sum(pnls) / len(pnls), 4),
            "best_slot": round(max(pnls), 4),
            "worst_slot": round(min(pnls), 4),
            "log_file": str(self._session_file),
        }

    # ── Chart generation ──────────────────────────────────────────────────

    def generate_charts(self, output_dir: Optional[Path] = None) -> List[Path]:
        """
        Generate one interactive HTML chart per completed slot.
        Returns list of created file paths.
        """
        out = Path(output_dir) if output_dir else self.log_dir / "charts"
        out.mkdir(parents=True, exist_ok=True)

        paths = []
        for entry in self._slots:
            if entry["type"] != "slot":
                continue
            path = self._render_slot_chart(entry, out)
            if path:
                paths.append(path)

        return paths

    def generate_session_chart(self, output_dir: Optional[Path] = None) -> Optional[Path]:
        """Generate a cumulative PnL curve for the entire session."""
        slots = [e for e in self._slots if e["type"] == "slot"]
        if not slots:
            return None

        out = Path(output_dir) if output_dir else self.log_dir / "charts"
        out.mkdir(parents=True, exist_ok=True)

        cumulative = []
        running = 0.0
        for s in slots:
            running += s["pnl"]["total_pnl"]
            cumulative.append({
                "label": s["pnl"]["market_id"][:10],
                "slot_pnl": s["pnl"]["total_pnl"],
                "cumulative": running,
            })

        html = self._render_session_chart_html(cumulative)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        path = out / f"session_pnl_{ts}.html"
        path.write_text(html, encoding="utf-8")
        logger.info(f"Session chart written: {path}")
        return path

    # ── HTML rendering ────────────────────────────────────────────────────

    def _render_slot_chart(
        self, entry: Dict[str, Any], out: Path
    ) -> Optional[Path]:
        pnl = entry["pnl"]
        trades = entry["trades"]
        market_id = pnl["market_id"]

        buy_trades = [t for t in trades if t["action"] == "BUY"]
        sell_trades = [t for t in trades if t["action"] == "SELL"]

        def trace(data, label, color, symbol):
            xs = [t.get("timestamp", "") for t in data]
            ys = [t.get("price", 0.5) for t in data]
            sizes = [max(8, t.get("size_usd", 10)) for t in data]
            return {
                "x": xs, "y": ys, "mode": "markers",
                "name": label,
                "marker": {"color": color, "symbol": symbol, "size": sizes},
                "type": "scatter",
            }

        traces = []
        if buy_trades:
            traces.append(trace(buy_trades, "BUY", "#00cc66", "triangle-up"))
        if sell_trades:
            traces.append(trace(sell_trades, "SELL", "#ff4444", "triangle-down"))

        total_pnl = pnl["total_pnl"]
        pnl_color = "#00cc66" if total_pnl >= 0 else "#ff4444"

        html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Slot {market_id[:12]}</title>
  <script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
  <style>
    body {{ font-family: monospace; background: #0d1117; color: #e6edf3; margin: 20px; }}
    .header {{ display: flex; gap: 20px; margin-bottom: 10px; }}
    .card {{ background: #161b22; border: 1px solid #30363d; padding: 10px 16px; border-radius: 6px; }}
    .pnl {{ color: {pnl_color}; font-size: 1.4em; font-weight: bold; }}
  </style>
</head>
<body>
  <div class="header">
    <div class="card"><b>Market</b><br>{market_id[:16]}</div>
    <div class="card"><b>Strategy</b><br>{pnl.get("strategy","?")}</div>
    <div class="card"><b>PnL</b><br><span class="pnl">${total_pnl:+.4f}</span></div>
    <div class="card"><b>Trades</b><br>{len(trades)}</div>
    <div class="card"><b>Simulation</b><br>{"YES" if pnl.get("simulation") else "NO"}</div>
  </div>
  <div id="chart"></div>
  <script>
    Plotly.newPlot("chart", {json.dumps(traces)}, {{
      paper_bgcolor: "#0d1117",
      plot_bgcolor: "#161b22",
      font: {{ color: "#e6edf3" }},
      xaxis: {{ title: "Time", gridcolor: "#30363d" }},
      yaxis: {{ title: "Price (0-1)", range: [0, 1], gridcolor: "#30363d" }},
      title: "Slot: {market_id[:20]}",
      legend: {{ bgcolor: "#161b22", bordercolor: "#30363d", borderwidth: 1 }},
    }});
  </script>
</body>
</html>"""

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        path = out / f"slot_{market_id[:12]}_{ts}.html"
        path.write_text(html, encoding="utf-8")
        return path

    def _render_session_chart_html(self, cumulative: List[Dict]) -> str:
        labels = [c["label"] for c in cumulative]
        cum_pnl = [c["cumulative"] for c in cumulative]
        slot_pnl = [c["slot_pnl"] for c in cumulative]

        cum_trace = {
            "x": labels, "y": cum_pnl,
            "mode": "lines+markers",
            "name": "Cumulative PnL",
            "line": {"color": "#58a6ff", "width": 2},
            "type": "scatter",
        }
        bar_trace = {
            "x": labels, "y": slot_pnl,
            "name": "Slot PnL",
            "marker": {
                "color": ["#00cc66" if v >= 0 else "#ff4444" for v in slot_pnl]
            },
            "type": "bar",
            "yaxis": "y2",
        }

        total = cum_pnl[-1] if cum_pnl else 0
        color = "#00cc66" if total >= 0 else "#ff4444"

        return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Session PnL — {self.strategy_name}</title>
  <script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
  <style>
    body {{ font-family: monospace; background: #0d1117; color: #e6edf3; margin: 20px; }}
    .summary {{ display: flex; gap: 20px; margin-bottom: 16px; }}
    .card {{ background: #161b22; border: 1px solid #30363d; padding: 10px 16px; border-radius: 6px; }}
    .pnl {{ color: {color}; font-size: 1.6em; font-weight: bold; }}
  </style>
</head>
<body>
  <div class="summary">
    <div class="card"><b>Strategy</b><br>{self.strategy_name}</div>
    <div class="card"><b>Total Slots</b><br>{len(cumulative)}</div>
    <div class="card"><b>Total PnL</b><br><span class="pnl">${total:+.4f}</span></div>
  </div>
  <div id="chart"></div>
  <script>
    Plotly.newPlot("chart",
      [{json.dumps(cum_trace)}, {json.dumps(bar_trace)}],
      {{
        paper_bgcolor: "#0d1117",
        plot_bgcolor: "#161b22",
        font: {{ color: "#e6edf3" }},
        xaxis: {{ title: "Market Slot", gridcolor: "#30363d" }},
        yaxis: {{ title: "Cumulative PnL (USD)", gridcolor: "#30363d" }},
        yaxis2: {{ title: "Slot PnL (USD)", overlaying: "y", side: "right",
                   gridcolor: "#30363d" }},
        title: "Session PnL — {self.strategy_name}",
        legend: {{ bgcolor: "#161b22", bordercolor: "#30363d", borderwidth: 1 }},
      }}
    );
  </script>
</body>
</html>"""

    # ── Internal helpers ──────────────────────────────────────────────────

    def _append_jsonl(self, entry: Dict[str, Any]) -> None:
        try:
            with open(self._session_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as exc:
            logger.error(f"Failed to write log entry: {exc}")

    @staticmethod
    def _receipt_to_dict(r: OrderReceipt) -> Dict[str, Any]:
        return {
            "order_id": r.order_id,
            "action": "SELL" if getattr(r, "is_sell", False) else "BUY",
            "side": r.side.value,
            "price": round(r.price, 4),
            "size_usd": round(r.size_usd, 4),
            "filled_usd": round(r.filled_size_usd, 4),
            "result": r.result.value,
            "timestamp": r.timestamp.isoformat(),
        }

    @classmethod
    def load_session(cls, log_file: Path) -> List[Dict[str, Any]]:
        """Load a previously saved session JSONL file."""
        entries = []
        with open(log_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        return entries
