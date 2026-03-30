"""
risk_controls.py — Advanced risk controls beyond the basic risk_manager.

1. Equity drawdown monitor — pauses trading if portfolio drops >5% in a day
2. Rolling win rate tracker — auto-reduces position size if WR drops below 65%
3. Persists state to JSON so it survives restarts

These controls are checked in main.py each cycle BEFORE any trading.
"""
import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

import config

logger = logging.getLogger(__name__)

_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "risk_state.json")

# ── Thresholds ────────────────────────────────────────────────────────

EQUITY_DRAWDOWN_PCT = 0.05       # 5% daily drawdown → pause
WIN_RATE_FLOOR = 0.65            # Below 65% over 7 days → reduce position size
WIN_RATE_REDUCED_POSITION = 2.00 # $2 max when win rate is low
WIN_RATE_WINDOW_DAYS = 7         # Rolling window for win rate


class RiskControls:
    """Advanced risk controls that operate above the basic RiskManager."""

    def __init__(self):
        self._start_of_day_equity: Optional[float] = None
        self._equity_date: Optional[str] = None
        self._paused: bool = False
        self._pause_reason: str = ""
        # Rolling trade outcomes: list of (timestamp, won: bool)
        self._trade_log: list[dict] = []
        self._load_state()

    # ── State persistence ─────────────────────────────────────────────

    def _load_state(self):
        if os.path.exists(_STATE_FILE):
            try:
                with open(_STATE_FILE) as f:
                    data = json.load(f)
                self._start_of_day_equity = data.get("start_of_day_equity")
                self._equity_date = data.get("equity_date")
                self._paused = data.get("paused", False)
                self._pause_reason = data.get("pause_reason", "")
                self._trade_log = data.get("trade_log", [])
                logger.info("Risk controls loaded: equity_date=%s, paused=%s, trades=%d",
                           self._equity_date, self._paused, len(self._trade_log))
            except Exception as e:
                logger.warning("Failed to load risk state: %s", e)

    def _save_state(self):
        try:
            data = {
                "start_of_day_equity": self._start_of_day_equity,
                "equity_date": self._equity_date,
                "paused": self._paused,
                "pause_reason": self._pause_reason,
                "trade_log": self._trade_log[-200:],  # Keep last 200 trades
            }
            with open(_STATE_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning("Failed to save risk state: %s", e)

    # ── Equity drawdown monitor ───────────────────────────────────────

    def check_equity_drawdown(self, current_equity: float) -> bool:
        """Check if equity has dropped >5% today. Returns True if trading should pause.
        
        Call at the start of each cycle with current (cash + portfolio_value).
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # First check of the day: record starting equity
        if self._equity_date != today:
            self._start_of_day_equity = current_equity
            self._equity_date = today
            self._paused = False  # Reset pause at start of new day
            self._pause_reason = ""
            self._save_state()
            logger.info("New day equity baseline: $%.2f", current_equity)
            return False

        if self._start_of_day_equity is None or self._start_of_day_equity <= 0:
            return False

        drawdown = (self._start_of_day_equity - current_equity) / self._start_of_day_equity

        if drawdown >= EQUITY_DRAWDOWN_PCT:
            self._paused = True
            self._pause_reason = (
                f"Equity drawdown {drawdown:.1%}: "
                f"${self._start_of_day_equity:.2f} → ${current_equity:.2f}"
            )
            self._save_state()
            logger.warning("EQUITY DRAWDOWN PAUSE: %s", self._pause_reason)
            return True

        return False

    # ── Rolling win rate tracker ──────────────────────────────────────

    def record_trade(self, ticker: str, won: bool, pnl: float = 0):
        """Record a completed trade outcome for win rate tracking."""
        self._trade_log.append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "ticker": ticker,
            "won": won,
            "pnl": round(pnl, 4),
        })
        # Prune old entries
        cutoff = (datetime.now(timezone.utc).timestamp() - 
                  WIN_RATE_WINDOW_DAYS * 86400)
        self._trade_log = [
            t for t in self._trade_log
            if datetime.fromisoformat(t["ts"]).timestamp() > cutoff
        ]
        self._save_state()

    def get_rolling_win_rate(self) -> Optional[float]:
        """Get win rate over the last WIN_RATE_WINDOW_DAYS days.
        Returns None if fewer than 10 trades in the window.
        """
        cutoff = (datetime.now(timezone.utc).timestamp() - 
                  WIN_RATE_WINDOW_DAYS * 86400)
        recent = [
            t for t in self._trade_log
            if datetime.fromisoformat(t["ts"]).timestamp() > cutoff
        ]
        if len(recent) < 10:
            return None  # Not enough data
        wins = sum(1 for t in recent if t["won"])
        return wins / len(recent)

    def get_adjusted_position_size(self) -> float:
        """Return the position size, reduced if win rate is below floor.
        
        Returns config.MAX_POSITION_USD normally, or WIN_RATE_REDUCED_POSITION
        if rolling win rate has dropped below WIN_RATE_FLOOR.
        """
        wr = self.get_rolling_win_rate()
        if wr is not None and wr < WIN_RATE_FLOOR:
            logger.warning(
                "Win rate %.0f%% < %.0f%% floor — reducing position size to $%.2f",
                wr * 100, WIN_RATE_FLOOR * 100, WIN_RATE_REDUCED_POSITION,
            )
            return WIN_RATE_REDUCED_POSITION
        return config.MAX_POSITION_USD

    # ── Status ────────────────────────────────────────────────────────

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def pause_reason(self) -> str:
        return self._pause_reason

    def unpause(self):
        """Manually unpause trading."""
        self._paused = False
        self._pause_reason = ""
        self._save_state()

    def status_summary(self) -> str:
        wr = self.get_rolling_win_rate()
        wr_str = f"{wr:.0%}" if wr is not None else "n/a (<10 trades)"
        pos_size = self.get_adjusted_position_size()
        return (
            f"7d WR: {wr_str} | "
            f"Position size: ${pos_size:.2f} | "
            f"Paused: {self._paused}"
        )


# Module-level singleton
risk_controls = RiskControls()
