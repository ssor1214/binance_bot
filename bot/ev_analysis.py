"""[2026-08-09] 거래 원장(trade_ledger)에 쌓인 실제 거래 기록으로 조건별(심볼/방향/시간대)
승률·평균손익·profit factor·기대값을 계산한다.

단순 전체 승률 최적화가 아니라 "이 조건은 수수료까지 반영한 기대값이 마이너스다"를 걸러낼 수
있게 하는 게 목적이다. 표본이 너무 적은 조건은 통계적으로 의미가 없으므로 결과에서 제외한다
(요청사항: "최소 표본 수 미만의 조건은 자동으로 사이즈를 늘리거나 최적화하지 않는다").
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class SegmentStats:
    key: str
    n: int
    win_rate: float
    avg_win_pct: float
    avg_loss_pct: float
    expectancy_pct: float  # 승률*평균승리 - 패률*평균손실 (수수료 반영 전)
    profit_factor: float | None  # 총이익/총손실(손실 0이면 None)
    max_consecutive_losses: int
    max_drawdown_usdt: float
    sufficient_sample: bool


def _segment_by(records: list[dict], key_fn) -> dict:
    groups = defaultdict(list)
    for r in records:
        groups[key_fn(r)].append(r)
    return groups


def _compute_segment_stats(key: str, records: list[dict], min_sample: int, fee_rate_roundtrip_pct: float) -> SegmentStats:
    n = len(records)
    pnls = [r["estimated_pnl_pct"] for r in records]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    win_rate = len(wins) / n if n else 0.0
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    loss_rate = 1 - win_rate
    expectancy = win_rate * avg_win + loss_rate * avg_loss - fee_rate_roundtrip_pct

    total_win_usdt = sum(r["estimated_pnl_usdt"] for r in records if r["estimated_pnl_usdt"] > 0)
    total_loss_usdt = -sum(r["estimated_pnl_usdt"] for r in records if r["estimated_pnl_usdt"] <= 0)
    profit_factor = (total_win_usdt / total_loss_usdt) if total_loss_usdt > 0 else None

    # 최대 연속손실 및 최대낙폭(원장에 기록된 순서 = 시간순이라고 가정)
    max_consec = 0
    cur_consec = 0
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in records:
        pnl_usdt = r["estimated_pnl_usdt"]
        if pnl_usdt <= 0:
            cur_consec += 1
            max_consec = max(max_consec, cur_consec)
        else:
            cur_consec = 0
        cum += pnl_usdt
        peak = max(peak, cum)
        max_dd = min(max_dd, cum - peak)

    return SegmentStats(
        key=key, n=n, win_rate=win_rate, avg_win_pct=avg_win, avg_loss_pct=avg_loss,
        expectancy_pct=expectancy, profit_factor=profit_factor,
        max_consecutive_losses=max_consec, max_drawdown_usdt=max_dd,
        sufficient_sample=n >= min_sample,
    )


def analyze_by_symbol(records: list[dict], min_sample: int = 10, fee_rate_roundtrip_pct: float = 0.1) -> list[SegmentStats]:
    groups = _segment_by(records, lambda r: r["symbol"])
    return sorted(
        (_compute_segment_stats(k, v, min_sample, fee_rate_roundtrip_pct) for k, v in groups.items()),
        key=lambda s: s.expectancy_pct,
    )


def analyze_by_side(records: list[dict], min_sample: int = 10, fee_rate_roundtrip_pct: float = 0.1) -> list[SegmentStats]:
    groups = _segment_by(records, lambda r: r["side"])
    return sorted(
        (_compute_segment_stats(k, v, min_sample, fee_rate_roundtrip_pct) for k, v in groups.items()),
        key=lambda s: s.expectancy_pct,
    )


def analyze_by_hour_utc(records: list[dict], min_sample: int = 10, fee_rate_roundtrip_pct: float = 0.1) -> list[SegmentStats]:
    def hour_key(r):
        dt = datetime.fromtimestamp(r["entered_at"], tz=timezone.utc)
        return f"{dt.hour:02d}시(UTC)"
    groups = _segment_by(records, hour_key)
    return sorted(
        (_compute_segment_stats(k, v, min_sample, fee_rate_roundtrip_pct) for k, v in groups.items()),
        key=lambda s: s.expectancy_pct,
    )


def negative_ev_segments(segments: list[SegmentStats]) -> list[SegmentStats]:
    """표본이 충분한데(sufficient_sample=True) 기대값이 마이너스인 조건만 골라낸다 —
    이런 조건은 자동 제외(진입 스킵) 후보로 쓸 수 있다. 표본 부족한 조건은 절대 포함하지 않는다."""
    return [s for s in segments if s.sufficient_sample and s.expectancy_pct < 0]
