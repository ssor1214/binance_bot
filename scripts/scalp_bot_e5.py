"""e5: F1 band-width gate over the existing CM direction execution path.

F1 is a selector, not a directional signal.  This module therefore reuses only
the execution/protection plumbing from e3 and rejects CM signals unless the
signal candle's 20-bar Bollinger width is at least ``--f1-min-pct``.

Safety: dry-run is the default.  Live orders require the explicit ``--e5-live``
flag and e3's acknowledgement flag.  e5 writes its own ledger/state/PID/log
files and must attach to the existing WS cache when shadowing e3.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import scalp_bot_e3 as _e3

ROOT = Path(__file__).resolve().parent.parent
_e3.VERSION = "e5"
_e3.LEDGER = ROOT / "logs" / "scalp_bot_e5_f1_ledger.jsonl"
_e3.STATE = ROOT / "logs" / "scalp_bot_e5_f1_state.json"
_e3.WS_PID_FILE = ROOT / "logs" / "scalp_bot_e5_f1_ws_pid.json"
_e3.BOT_PID_FILE = ROOT / "logs" / "scalp_bot_e5_f1_bot_pid.json"
_e3.RUN_LOG = ROOT / "logs" / "scalp_bot_e5_f1_run.log"


class E5Telegram(_e3.Tg):
    """e5-only Telegram menu; never consumes e3 callback/button commands."""
    BUTTONS = {
        "📊 e5상태": "status",
        "📈 e5브리핑": "brief",
        "📋 e5포지션": "pos",
        "📉 e5복기": "review",
        "⏸ e5정지": "pause",
        "▶️ e5재개": "resume",
        "🛑 e5전량청산": "flat",
    }

    def menu(self) -> None:
        self.send("[e5] 조작 메뉴를 하단에 고정했습니다. 언제든 누르세요.", {
            "keyboard": [
                [{"text": "📊 e5상태"}, {"text": "📈 e5브리핑"}],
                [{"text": "📋 e5포지션"}, {"text": "📉 e5복기"}],
                [{"text": "⏸ e5정지"}, {"text": "▶️ e5재개"}],
                [{"text": "🛑 e5전량청산"}],
            ],
            "resize_keyboard": True,
            "is_persistent": True,
        })

    def poll(self) -> list:
        r = self._api("getUpdates", {"offset": self.offset, "timeout": 0,
                                     "allowed_updates": ["callback_query", "message"]},
                      timeout=8)
        out = []
        if not r or not r.get("ok"):
            return out
        for u in r.get("result", []):
            self.offset = u["update_id"] + 1
            cq = u.get("callback_query")
            if cq and str(cq.get("data", "")).startswith("e5:"):
                out.append((cq["id"], cq["data"][3:]))
                continue
            txt = ((u.get("message") or {}).get("text") or "").strip()
            if txt == "/e5" or txt == "e5" or txt == "/menu":
                self.menu()
            elif txt in self.BUTTONS:
                out.append(("", self.BUTTONS[txt]))
        return out


def f1_band_width_series(df):
    """Return the rolling 20-bar Bollinger-width percentage series."""
    import numpy as np
    if df is None or len(df) < 20:
        return np.array([], dtype=float)
    close = np.asarray(df["close"], dtype=float)
    out = np.full(len(close), np.nan)
    for i in range(19, len(close)):
        w = close[i - 19:i + 1]
        mean = float(np.mean(w))
        if mean > 0:
            out[i] = 4.0 * float(np.std(w)) / mean * 100.0
    return out


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--f1-low-pct", type=float, default=92.0,
                        help="F1 lower percentile of selected 47/50 band")
    parser.add_argument("--f1-high-pct", type=float, default=94.0,
                        help="F1 upper percentile of selected 47/50 band")
    parser.add_argument("--f1-lookback-bars", type=int, default=1440,
                        help="per-symbol historical bars used for live F1 percentile")
    parser.add_argument("--e5-live", action="store_true",
                        help="allow live orders; omitted means dry-run")
    own, rest = parser.parse_known_args()
    sys.argv = [sys.argv[0], *rest]

    original = _e3.cm_signal_snapshot
    diag = {"evaluated": 0, "passed": 0, "rejected": 0, "last_log": time.time()}

    def gated_snapshot(ex, symbol, args, chart_df=None):
        sig = original(ex, symbol, args, chart_df=chart_df)
        if not sig or not sig.get("signal"):
            return sig
        diag["evaluated"] += 1
        widths = f1_band_width_series(chart_df)
        if len(widths) < 20 or widths[-1] != widths[-1]:
            diag["rejected"] += 1
            return None
        sample = widths[-max(20, own.f1_lookback_bars):]
        sample = sample[~__import__("numpy").isnan(sample)]
        if len(sample) < 20:
            diag["rejected"] += 1
            return None
        rank = float((sample < widths[-1]).sum()) / len(sample) * 100.0
        if not (own.f1_low_pct <= rank < own.f1_high_pct):
            diag["rejected"] += 1
            return None
        diag["passed"] += 1
        if time.time() - diag["last_log"] >= 60:
            _e3.log_line(
                f"e5 F1계측 평가{diag['evaluated']} 통과{diag['passed']} "
                f"차단{diag['rejected']} 구간{own.f1_low_pct:.0f}~{own.f1_high_pct:.0f}%")
            diag["last_log"] = time.time()
        sig["e5_f1_width_pct"] = float(widths[-1])
        sig["e5_f1_percentile"] = rank
        return sig

    _e3.cm_signal_snapshot = gated_snapshot
    _e3.Tg = E5Telegram
    _e3.RUNTIME_STATUS_EXTRA = lambda: (
        f"감시: CM 방향신호 → 1분봉 눌림 → F1 밴드폭 {own.f1_low_pct:.0f}~"
        f"{own.f1_high_pct:.0f}% 선별 → 지정가 진입 시도\n"
        f"F1계측: 평가{diag['evaluated']} 통과{diag['passed']} 차단{diag['rejected']}"
    )
    if "--instance-tag" not in sys.argv:
        sys.argv += ["--instance-tag", "e5"]
    if "--attach-ws" not in sys.argv:
        sys.argv += ["--attach-ws"]
    # F1 validation was 1-minute signal bars with a 15-bar (15-minute)
    # holding horizon. Keep e5 aligned with that measurement and cap it at
    # the user's requested eight concurrent slots unless explicitly changed.
    if "--signal-tf-min" not in sys.argv:
        sys.argv += ["--signal-tf-min", "1"]
    if "--entry-tf-min" not in sys.argv:
        sys.argv += ["--entry-tf-min", "1"]
    if "--max-concurrency" not in sys.argv:
        sys.argv += ["--max-concurrency", "8"]
    if "--dynamic-min-leg-margin" not in sys.argv:
        sys.argv += ["--dynamic-min-leg-margin"]
    if "--skip-log-sec" not in sys.argv:
        sys.argv += ["--skip-log-sec", "60"]
    if "--max-signal-age" not in sys.argv:
        sys.argv += ["--max-signal-age", "20"]
    if "--brief-on-clock" not in sys.argv:
        sys.argv += ["--brief-on-clock"]
    if not own.e5_live and "--dry-run" not in sys.argv:
        sys.argv += ["--dry-run"]
    if own.e5_live and "--i-know-it-loses" not in sys.argv:
        sys.argv += ["--i-know-it-loses"]
    return int(_e3.main())


if __name__ == "__main__":
    raise SystemExit(main())
