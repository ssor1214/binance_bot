"""진입 다리 소실 회귀 방지 — 체결 조회 창은 entered_at 기준 5초로는 부족하다.

배경(2026-09-01): 주 진입 경로(안전망, 유입의 93%)는 Pos 를 만들 때
entered_at=now_ts, 즉 **폴링이 발견한 시각**을 쓴다. 실제 체결은 그보다 앞선다.
종전 창(5초)은 진입 체결을 놓쳐 real_commission 에 청산 다리만 남겼다
(원장 1,017건 중 377건=37%).
"""
import importlib.util
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load():
    """무거운 임포트 없이 모듈 상수/함수만 얻는다."""
    if "scalp_bot_e3" in sys.modules:
        return sys.modules["scalp_bot_e3"]
    spec = importlib.util.spec_from_file_location(
        "scalp_bot_e3", ROOT / "scripts" / "scalp_bot_e3.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["scalp_bot_e3"] = mod
    spec.loader.exec_module(mod)
    return mod


M = _load()


def _pos(entered_at, since_trade_id):
    p = types.SimpleNamespace()
    p.entered_at = entered_at
    p.since_trade_id = since_trade_id
    return p


def test_wide_window_when_since_id_present():
    """since_trade_id 가 경계를 잡아 주므로 창은 넓어야 한다."""
    t = 1_800_000_000.0
    got = M.trades_start_ms(_pos(t, 12345))
    assert got == int(t * 1000) - M.WIDE_TRADE_LOOKBACK_MS
    # 눌림 지정가가 30분 대기 후 체결된 경우도 창 안에 들어와야 한다.
    fill_ms = int((t - 30 * 60) * 1000)
    assert got < fill_ms


def test_narrow_window_when_no_since_id():
    """필터가 없으면 창까지 넓히면 안 된다 — 이전 포지션 체결이 섞인다."""
    t = 1_800_000_000.0
    assert M.trades_start_ms(_pos(t, 0)) == int(t * 1000) - 5000


def test_lookback_covers_realistic_pullback_wait():
    """폴링 주기 + 눌림 대기를 넉넉히 덮는가."""
    assert M.WIDE_TRADE_LOOKBACK_MS >= 60 * 60 * 1000
