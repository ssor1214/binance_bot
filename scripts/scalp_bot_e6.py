"""e6: Bollinger mean-reversion live candidate.

Reuses e5/e3 protection, TTL, ledger and Telegram plumbing, but replaces the
CM direction with a Bollinger-touch counter-trend signal and uses the middle
band as the planned take-profit.  Live mode remains explicit.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import scalp_bot_e5 as _e5
import scalp_bot_e3 as _e3

ROOT = Path(__file__).resolve().parent.parent
_e3.VERSION = "e6"
_e3.LEDGER = ROOT / "logs" / "scalp_bot_e6_ledger.jsonl"
_e3.STATE = ROOT / "logs" / "scalp_bot_e6_state.json"
_e3.WS_PID_FILE = ROOT / "logs" / "scalp_bot_e6_ws_pid.json"
_e3.BOT_PID_FILE = ROOT / "logs" / "scalp_bot_e6_bot_pid.json"
_e3.RUN_LOG = ROOT / "logs" / "scalp_bot_e6_run.log"
_e3.STRATEGY_LABEL = "볼밴 역추세+밴드 지정가"


class E6Telegram(_e5.E5Telegram):
    BUTTONS = {k.replace("e5", "e6"): v for k, v in _e5.E5Telegram.BUTTONS.items()}

    def menu(self) -> None:
        self.send("[e6] 볼밴 역추세 전용 메뉴를 하단에 고정했습니다.", {
            "keyboard": [
                [{"text": "📊 e6상태"}, {"text": "📈 e6브리핑"}],
                [{"text": "📋 e6포지션"}, {"text": "📉 e6복기"}],
                [{"text": "⏸ e6정지"}, {"text": "▶️ e6재개"}],
                [{"text": "🛑 e6전량청산"}],
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
            if cq and str(cq.get("data", "")).startswith("e6:"):
                out.append((cq["id"], cq["data"][3:]))
                continue
            txt = ((u.get("message") or {}).get("text") or "").strip()
            if txt in ("/e6", "e6", "/menu"):
                self.menu()
            elif txt in self.BUTTONS:
                # Acknowledge at polling time; the market scan may be busy for
                # several seconds before the main-loop action handler runs.
                self.send(f"[e6] {txt} 수신 — 상태를 조회하는 중입니다.")
                out.append(("", self.BUTTONS[txt]))
        return out


def bb_signal(ex, symbol, args, chart_df=None):
    if chart_df is None or len(chart_df) < 25:
        return None
    ind = _e3.indicators(chart_df)
    if not ind:
        return None
    row = chart_df.iloc[-1]
    low = float(row["low"]); high = float(row["high"])
    lo = float(ind.get("bb_l") or 0); hi = float(ind.get("bb_u") or 0)
    # e3's legacy indicator payload exposes bb_u/bb_l but not the SMA20.
    # e6 must derive the middle band explicitly; otherwise every signal is
    # rejected by the mid>0 guard and the live bot records zero touches.
    mid = float(ind.get("bb_mid") or (sum(float(x) for x in chart_df["close"].iloc[-20:]) / 20.0))
    if not (lo > 0 and hi > 0 and mid > 0):
        return None
    reentry = max(0.0, float(globals().get("E6_REENTRY_PCT", 0.2))) / 100.0
    long_confirm = low <= lo and float(row["close"]) >= lo * (1.0 + reentry)
    short_confirm = high >= hi and float(row["close"]) <= hi * (1.0 - reentry)
    side = "LONG" if long_confirm else ("SHORT" if short_confirm else "")
    if not side:
        return None
    out = dict(signal=side, cm_tp_long=mid, cm_tp_short=mid,
               bb_u=hi, bb_l=lo, bb_mid=mid)
    return out


def main() -> int:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--e6-live", action="store_true")
    p.add_argument("--f1-low-pct", type=float, default=80.0)
    p.add_argument("--f1-high-pct", type=float, default=100.0)
    p.add_argument("--reentry-pct", type=float, default=0.2)
    p.add_argument("--tp-frac", type=float, default=0.42)
    own, rest = p.parse_known_args()
    global E6_REENTRY_PCT, E6_TP_FRAC
    E6_REENTRY_PCT = own.reentry_pct
    E6_TP_FRAC = own.tp_frac
    sys.argv = [sys.argv[0], *rest]
    diag = {"bars": 0, "touch": 0, "f1": 0, "last": time.time()}
    # e3의 CM 익절값을 그대로 두면 e6의 볼밴 중심선 TP가 아니라
    # Hull/CM 계산값이 사용된다. e6에서는 모든 진입 경로의 TP 기준을
    # SMA20 중심선으로 통일한다.
    _base_indicators = _e3.indicators
    def e6_indicators(df):
        out = _base_indicators(df)
        if not out or df is None or len(df) < 20:
            return out
        mid = float(sum(float(x) for x in df["close"].iloc[-20:]) / 20.0)
        if mid > 0:
            out["bb_mid"] = mid
            out["cm_tp_long"] = mid
            out["cm_tp_short"] = mid
        return out
    _e3.indicators = e6_indicators
    _base_cm_tp_price = _e3.cm_tp_price
    def e6_cm_tp_price(ind, entry, side, pullback_pct, lev, max_roe):
        mid = float(ind.get("bb_mid") or 0.0)
        if mid > 0 and entry > 0 and ((side == "LONG" and mid > entry) or
                                      (side == "SHORT" and mid < entry)):
            frac = min(1.0, max(0.05, float(globals().get("E6_TP_FRAC", 0.42))))
            return entry + (mid - entry) * frac
        return _base_cm_tp_price(ind, entry, side, pullback_pct, lev, max_roe)
    _e3.cm_tp_price = e6_cm_tp_price
    def diag_log():
        if time.time() - diag["last"] >= 60:
            _e3.log_line(f"e6 계측 봉{diag['bars']} 볼밴접촉{diag['touch']} "
                         f"F1통과{diag['f1']} 구간{own.f1_low_pct:.0f}~{own.f1_high_pct:.0f}%")
            diag["last"] = time.time()
    def gated(ex, symbol, args, chart_df=None):
        diag["bars"] += 1
        sig = bb_signal(ex, symbol, args, chart_df)
        diag_log()
        if not sig:
            return None
        diag["touch"] += 1
        widths = _e5.f1_band_width_series(chart_df)
        if len(widths) < 20 or widths[-1] != widths[-1]:
            return None
        import numpy as np
        sample = widths[-1440:]
        sample = sample[~np.isnan(sample)]
        rank = float((sample < widths[-1]).sum()) / len(sample) * 100.0 if len(sample) else -1
        if not (own.f1_low_pct <= rank < own.f1_high_pct):
            return None
        diag["f1"] += 1
        sig["e6_f1_percentile"] = rank
        return sig

    def band_targets(ind, side, tranches, second_at_band, min_gap_pct=0.0):
        if tranches <= 0:
            return []
        return [float(ind["bb_l"] if side == "LONG" else ind["bb_u"])]

    _e3.cm_signal_snapshot = gated
    _e3.tranche_targets = band_targets
    _e3.Tg = E6Telegram
    sys.argv += ["--instance-tag", "e6"] if "--instance-tag" not in sys.argv else []
    sys.argv += ["--attach-ws"] if "--attach-ws" not in sys.argv else []
    sys.argv += ["--signal-tf-min", "1"] if "--signal-tf-min" not in sys.argv else []
    sys.argv += ["--entry-tf-min", "1"] if "--entry-tf-min" not in sys.argv else []
    sys.argv += ["--max-concurrency", "4"] if "--max-concurrency" not in sys.argv else []
    sys.argv += ["--max-same-side", "2"] if "--max-same-side" not in sys.argv else []
    sys.argv += ["--tranches", "1"] if "--tranches" not in sys.argv else []
    sys.argv += ["--tiered-min-leg-margin"]
    # e6 is fully independent of CM: no CM flip-age gate or confirmation path.
    sys.argv += ["--cm-flip-max-bars", "-1"] if "--cm-flip-max-bars" not in sys.argv else []
    sys.argv += ["--cm-tp-max-roe", "0"] if "--cm-tp-max-roe" not in sys.argv else []
    sys.argv += ["--cm-tp-pullback-pct", "0"] if "--cm-tp-pullback-pct" not in sys.argv else []
    # e3의 EMA25 손절은 추세추종 방향용이라 e6 역추세에서는 손절선이
    # 진입가의 반대편에 놓여 모든 후보가 손절선통과로 차단될 수 있다.
    # e6는 방향에 맞는 고정 손절(5배 기준 가격폭 0.6%)을 기본 사용한다.
    sys.argv += ["--stop-fixed-roe", "3.0"] if "--stop-fixed-roe" not in sys.argv else []
    # The band-touch timestamp, not the wall-clock second, governs e6 entry.
    # e3's 20-second signal-age gate discarded valid band orders before they
    # could be placed later in the sequential 85-symbol scan.
    sys.argv += ["--max-signal-age", "0"] if "--max-signal-age" not in sys.argv else []
    sys.argv += ["--brief-on-clock"] if "--brief-on-clock" not in sys.argv else []
    if not own.e6_live:
        sys.argv += ["--dry-run"]
    else:
        sys.argv += ["--i-know-it-loses"]
    return int(_e3.main())


if __name__ == "__main__":
    raise SystemExit(main())
