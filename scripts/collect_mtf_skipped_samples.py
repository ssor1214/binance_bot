"""MTF(상위 시간대 추세) 불일치로 건너뛴 후보를 표본화한다. **설정은 건드리지 않는다.**

배경: 진입 깔때기 실측에서 후보 70건 중 35건(50%)이 "상위 시간대 추세 불일치(0/2)"로
탈락했다. 이 필터를 완화하면 거래량이 늘지만 승률이 깨질 수 있어, 완화 전에 먼저
"그때 진입했다면 어땠을까"를 실제 캔들로 확인하기 위한 표본 수집기다.

수집 방식:
- bot.log(+로테이션 백업)에서 "상위 시간대 추세 불일치" 줄과, 그 직전 "진입 후보 N개" 줄을
  짝지어 심볼/시각/확률/우선순위를 복원한다.
- 각 표본에 대해 스킵 시점 이후 실제 1분봉을 조회해 N분 뒤 수익률을 계산한다.
  (진입가는 스킵 직후 1분봉 시가로 근사 — 실제 지정가 체결과는 다를 수 있다)

**lookahead 없음**: 스킵 시각 이후의 캔들만 사용하며, 조건을 사후에 역산하지 않는다.
**REST 스로틀 0.4초** — 이 저장소에서 무스로틀 klines 반복호출로 실제 IP밴이 난 적 있다.

실행:
  python scripts/collect_mtf_skipped_samples.py --since "2026-08-16 00:00"
  python scripts/collect_mtf_skipped_samples.py --since "2026-08-16 00:00" --no-price  # 로그만
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import statistics
import time
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
LOG_PATHS = [ROOT / "logs" / "bot.log", ROOT / "logs" / "bot.log.1", ROOT / "logs" / "bot.log.2"]
OUT_PATH = ROOT / "logs" / "mtf_skipped_samples.jsonl"
BASE_URL = "https://fapi.binance.com/fapi/v1/klines"
THROTTLE_SEC = 0.4

TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+")
SKIP_RE = re.compile(r"\[([A-Z0-9가-힣龙虾]+USDT)\] 상위 시간대 추세 불일치\((\d)/(\d)\)")
CAND_RE = re.compile(r"이번 주기 진입 후보 \d+개 \(상위: (\[.*?\]), 확률/score순")


def parse_ts(line: str) -> float | None:
    m = TS_RE.match(line)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()


def parse_candidate_list(line: str) -> dict[str, tuple[float, float]]:
    """'상위: [(SYM, prob, priority), ...]' 를 {심볼: (확률, 우선순위)}로."""
    m = CAND_RE.search(line)
    if not m:
        return {}
    raw = m.group(1)
    # np.float64(0.67) 같은 표기를 숫자로 바꾼다
    raw = re.sub(r"np\.float64\(([-\d.eE]+)\)", r"\1", raw)
    try:
        items = ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return {}
    out = {}
    for it in items:
        if isinstance(it, (list, tuple)) and len(it) >= 3:
            try:
                out[str(it[0])] = (float(it[1]), float(it[2]))
            except (TypeError, ValueError):
                continue
    return out


def collect(since_ts: float) -> list[dict]:
    """스킵 로그를 모으고, 같은 스캔 주기의 후보 로그에서 확률/우선순위를 붙인다."""
    samples: list[dict] = []
    for path in LOG_PATHS:
        if not path.exists():
            continue
        recent_cands: dict[str, tuple[float, float]] = {}
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                ts = parse_ts(line)
                if ts is None:
                    continue
                if "이번 주기 진입 후보" in line:
                    parsed = parse_candidate_list(line)
                    if parsed:
                        recent_cands = parsed
                    continue
                m = SKIP_RE.search(line)
                if not m or ts < since_ts:
                    continue
                symbol = m.group(1)
                prob, prio = recent_cands.get(symbol, (None, None))
                samples.append({
                    "skipped_at": ts,
                    "skipped_at_str": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
                    "symbol": symbol,
                    "mtf_agree": int(m.group(2)),
                    "mtf_total": int(m.group(3)),
                    "probability": prob,
                    "entry_priority": prio,
                    "source_log": path.name,
                })
    samples.sort(key=lambda s: s["skipped_at"])
    return samples


def fetch_klines(symbol: str, start_ms: int, end_ms: int) -> list[list]:
    try:
        r = requests.get(BASE_URL, params={
            "symbol": symbol, "interval": "1m",
            "startTime": start_ms, "endTime": end_ms, "limit": 60,
        }, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception:
        return []
    finally:
        time.sleep(THROTTLE_SEC)  # IP밴 방지 — 실패해도 반드시 쉰다


def save(samples: list[dict]) -> None:
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        for s in samples:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")


def attach_forward_returns(samples: list[dict], horizons=(1, 3, 5, 10), all_samples=None) -> None:
    """스킵 직후 진입했다고 가정하고 N분 뒤 수익률(%)을 붙인다.
    진입가는 스킵 다음 1분봉 시가로 근사한다(실제 지정가 체결과는 다를 수 있음)."""
    total = len(samples)
    for i, s in enumerate(samples, 1):
        start_ms = int((s["skipped_at"] // 60 + 1) * 60 * 1000)
        end_ms = start_ms + (max(horizons) + 2) * 60 * 1000
        kl = fetch_klines(s["symbol"], start_ms, end_ms)
        if not kl:
            s["forward"] = None
            continue
        entry = float(kl[0][1])  # 다음 1분봉 시가
        s["assumed_entry"] = entry
        fwd = {}
        for h in horizons:
            if len(kl) > h:
                close = float(kl[h][4])
                fwd[f"{h}m"] = round((close - entry) / entry * 100, 4)
        s["forward"] = fwd
        if i % 25 == 0:
            # [2026-08-17] 맨 끝에서만 저장하면 중단 시 전부 날아간다(실제로 timeout에 걸려
            # 435건 조회분을 통째로 잃었다). 25건마다 중간 저장한다.
            save(all_samples if all_samples is not None else samples)
            print(f"  ... {i}/{total} 처리 (중간 저장)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", type=str, required=True, help='"YYYY-MM-DD HH:MM"')
    ap.add_argument("--no-price", action="store_true", help="캔들 조회 없이 로그만 수집")
    ap.add_argument("--limit", type=int, default=None, help="캔들 조회할 표본 수 상한(REST 절약)")
    args = ap.parse_args()

    since_ts = datetime.strptime(args.since, "%Y-%m-%d %H:%M").timestamp()
    samples = collect(since_ts)
    print(f"MTF 스킵 표본 {len(samples)}건 수집 ({args.since} 이후)")
    if not samples:
        return

    with_meta = [s for s in samples if s["probability"] is not None]
    print(f"  확률/우선순위 복원 성공: {len(with_meta)}건 ({len(with_meta)/len(samples)*100:.0f}%)")

    if not args.no_price:
        target = samples[: args.limit] if args.limit else samples
        print(f"  캔들 조회 {len(target)}건 (0.4초 스로틀, 약 {len(target)*0.4/60:.1f}분 소요)")
        attach_forward_returns(target, all_samples=samples)

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        for s in samples:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    print(f"저장: {OUT_PATH}")

    scored = [s for s in samples if s.get("forward")]
    if scored:
        print()
        print("=== 스킵된 후보를 진입했다면 (LONG 가정, 1분봉 근사) ===")
        for h in ("1m", "3m", "5m", "10m"):
            vals = [s["forward"][h] for s in scored if h in (s.get("forward") or {})]
            if not vals:
                continue
            wins = sum(1 for v in vals if v > 0)
            print(f"  {h:>3}: n={len(vals):3d} 평균 {statistics.mean(vals):+.3f}% "
                  f"중앙값 {statistics.median(vals):+.3f}% 상승비율 {wins/len(vals)*100:.1f}%")
        print()
        print("[주의] 이 수치는 '진입 후 N분 뒤 가격'일 뿐, 실제 봇의 익절/손절 로직을 거친")
        print("       결과가 아니다. 필터 완화 판단에는 offline_backtest.py 재현이 추가로 필요하다.")


if __name__ == "__main__":
    main()
