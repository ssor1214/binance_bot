"""[2026-08-12 사용자요청] "스마트폰 사파리로 볼 수 있는 실시간 대시보드" — 로컬에서
읽기 전용 HTTP 서버를 띄우고, Cloudflare Tunnel로 외부에서 접속 가능한 URL을 만든다.
라이브 봇 프로세스와는 완전히 분리된 별도 프로세스(같은 .bot_stats.json/trade_ledger.jsonl
파일과 거래소 REST만 읽음, 주문/설정 변경 없음 — 100% 읽기 전용)."""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.config import Config
from bot.exchange import Exchange

PORT = 8787
KST = timezone(timedelta(hours=9))

cfg = Config()
ex = Exchange(cfg)

ROTATION_START_TS = time.mktime(time.strptime("2026-08-11 10:53:48", "%Y-%m-%d %H:%M:%S"))


def load_stats() -> dict:
    try:
        return json.loads((ROOT / ".bot_stats.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_recent_trades(limit=12) -> list[dict]:
    rows = []
    try:
        with open(ROOT / "logs" / "trade_ledger.jsonl", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    rows.sort(key=lambda r: r.get("exited_at", 0))
    return rows[-limit:][::-1]


def today_stats() -> dict:
    today_start = datetime.now(KST).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    rows = []
    try:
        with open(ROOT / "logs" / "trade_ledger.jsonl", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("origin", "bot") == "bot" and r.get("entered_at", 0) >= today_start:
                    rows.append(r)
    except Exception:
        pass
    wins = [r for r in rows if (r.get("estimated_pnl_usdt") or 0) > 0]
    net = sum((r.get("estimated_pnl_usdt") or 0) for r in rows)
    win_rate = (len(wins) / len(rows) * 100) if rows else 0.0
    return {"count": len(rows), "win_rate": win_rate, "net": net}


def rotation_stats() -> dict:
    rows = []
    try:
        with open(ROOT / "logs" / "trade_ledger.jsonl", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("origin", "bot") == "bot" and r.get("entered_at", 0) >= ROTATION_START_TS:
                    rows.append(r)
    except Exception:
        pass
    wins = [r for r in rows if (r.get("estimated_pnl_usdt") or 0) > 0]
    net = sum((r.get("estimated_pnl_usdt") or 0) for r in rows)
    win_rate = (len(wins) / len(rows) * 100) if rows else 0.0
    return {"count": len(rows), "win_rate": win_rate, "net": net}


def live_snapshot() -> dict:
    try:
        balance = ex.get_total_margin_balance()
    except Exception:
        balance = None
    positions = []
    try:
        for p in ex.get_open_positions():
            try:
                p["mark_price"] = ex.get_mark_price(p["symbol"])
            except Exception:
                p["mark_price"] = p["entry_price"]
            positions.append(p)
    except Exception:
        pass
    return {"balance": balance, "positions": positions}


def heartbeat_age_sec() -> float | None:
    try:
        mtime = (ROOT / "logs" / "heartbeat.txt").stat().st_mtime
        return time.time() - mtime
    except Exception:
        return None


def fmt_usdt(v):
    if v is None:
        return "—"
    sign = "+" if v > 0 else ""
    return f"{sign}{v:,.2f}"


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


PAGE_TEMPLATE = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="20">
<title>스캘핑 관제 — Binance Futures</title>
<style>
:root {{
  --bg: #0a0e12;
  --surface: #12181f;
  --surface-2: #1a222b;
  --border: #232d38;
  --text: #e7edf3;
  --text-dim: #7d8b98;
  --text-faint: #4d5a66;
  --accent: #3ddc9a;
  --accent-dim: #1f7a55;
  --pos: #3ddc9a;
  --neg: #ff5c6c;
  --warn: #f0b429;
}}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; padding: 0; background: var(--bg); color: var(--text); }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Pretendard", sans-serif;
  font-size: 15px;
  line-height: 1.5;
  padding: 20px 16px 60px;
  max-width: 720px;
  margin: 0 auto;
}}
.num {{ font-variant-numeric: tabular-nums; font-feature-settings: "tnum"; }}
.mono {{ font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; }}

header {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 18px;
  flex-wrap: wrap;
}}
.title {{
  font-size: 13px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--text-dim);
  font-weight: 600;
}}
.status-pill {{
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 10px;
  border-radius: 100px;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.02em;
}}
.status-pill.live {{ background: rgba(61,220,154,0.12); color: var(--accent); }}
.status-pill.dead {{ background: rgba(255,92,108,0.14); color: var(--neg); }}
.dot {{ width: 6px; height: 6px; border-radius: 50%; background: currentColor; }}
.dot.live {{ animation: pulse 2s ease-in-out infinite; }}
@keyframes pulse {{ 0%,100% {{ opacity: 1; }} 50% {{ opacity: 0.35; }} }}
@media (prefers-reduced-motion: reduce) {{ .dot.live {{ animation: none; }} }}

.updated {{ font-size: 12px; color: var(--text-faint); }}

.stat-grid {{
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 10px;
  margin-bottom: 16px;
}}
.stat-tile {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 14px 14px 12px;
}}
.stat-label {{
  font-size: 11.5px;
  color: var(--text-dim);
  letter-spacing: 0.03em;
  margin-bottom: 6px;
}}
.stat-value {{
  font-size: 21px;
  font-weight: 700;
  letter-spacing: -0.01em;
}}
.stat-sub {{ font-size: 12px; color: var(--text-faint); margin-top: 3px; }}
.pos {{ color: var(--pos); }}
.neg {{ color: var(--neg); }}

section {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  margin-bottom: 12px;
  overflow: hidden;
}}
section > h2 {{
  font-size: 12px;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: var(--text-dim);
  font-weight: 600;
  margin: 0;
  padding: 12px 14px 10px;
  border-bottom: 1px solid var(--border);
}}
.section-body {{ padding: 12px 14px 14px; }}

table {{ width: 100%; border-collapse: collapse; font-size: 13.5px; }}
th {{
  text-align: left;
  font-size: 11px;
  color: var(--text-faint);
  font-weight: 600;
  padding: 6px 10px;
  letter-spacing: 0.02em;
}}
td {{ padding: 8px 10px; border-top: 1px solid var(--border); }}
tr:hover td {{ background: var(--surface-2); }}
.side-chip {{
  display: inline-block;
  padding: 1px 7px;
  border-radius: 5px;
  font-size: 11px;
  font-weight: 700;
}}
.side-chip.long {{ background: rgba(61,220,154,0.15); color: var(--pos); }}
.side-chip.short {{ background: rgba(255,92,108,0.15); color: var(--neg); }}
.overflow-x {{ overflow-x: auto; }}

.empty {{ padding: 20px 14px; text-align: center; color: var(--text-faint); font-size: 13px; }}

.safety-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
.safety-item .k {{ font-size: 12px; color: var(--text-dim); margin-bottom: 3px; }}
.safety-item .v {{ font-size: 15px; font-weight: 600; }}

footer {{ text-align: center; color: var(--text-faint); font-size: 11.5px; margin-top: 22px; }}
</style>
</head>
<body>
<header>
  <div>
    <div class="title">스캘핑 관제 · Binance USDT-M Futures</div>
  </div>
  <div style="display:flex; align-items:center; gap:10px;">
    <span class="status-pill {hb_class}"><span class="dot {hb_class}"></span>{hb_label}</span>
    <span class="updated num">{updated_at}</span>
  </div>
</header>

<div class="stat-grid">
  <div class="stat-tile">
    <div class="stat-label">총 자산</div>
    <div class="stat-value num">{balance} USDT</div>
  </div>
  <div class="stat-tile">
    <div class="stat-label">오늘 손익 ({today_count}건 · 승률{today_wr:.0f}%)</div>
    <div class="stat-value num {today_cls}">{today_net} USDT</div>
  </div>
  <div class="stat-tile">
    <div class="stat-label">순환매매 누적 ({rot_count}건 · 승률{rot_wr:.0f}%)</div>
    <div class="stat-value num {rot_cls}">{rot_net} USDT</div>
  </div>
</div>

<section>
  <h2>보유 포지션</h2>
  <div class="section-body overflow-x">
    {positions_html}
  </div>
</section>

<section>
  <h2>최근 체결 ({recent_count}건)</h2>
  <div class="section-body overflow-x">
    {recent_html}
  </div>
</section>

<section>
  <h2>안전장치</h2>
  <div class="section-body">
    <div class="safety-grid">
      <div class="safety-item">
        <div class="k">일일 손실 한도</div>
        <div class="v">{daily_loss_limit}</div>
      </div>
      <div class="safety-item">
        <div class="k">전역 연패 한도</div>
        <div class="v">{loss_streak_limit}</div>
      </div>
      <div class="safety-item">
        <div class="k">슬롯당 비중</div>
        <div class="v">{position_size}</div>
      </div>
      <div class="safety-item">
        <div class="k">동시 최대 포지션</div>
        <div class="v">{max_positions}</div>
      </div>
    </div>
  </div>
</section>

<footer>읽기 전용 대시보드 · 20초마다 자동 갱신 · 주문/설정 변경 기능 없음</footer>
</body>
</html>
"""


def render() -> str:
    stats = load_stats()
    live = live_snapshot()
    today = today_stats()
    rot = rotation_stats()
    recent = load_recent_trades()

    hb_age = heartbeat_age_sec()
    hb_alive = hb_age is not None and hb_age < 90
    hb_class = "live" if hb_alive else "dead"
    hb_label = "엔진 가동중" if hb_alive else "응답없음"

    balance_str = f"{live['balance']:,.2f}" if live["balance"] is not None else "—"

    positions = live["positions"]
    if positions:
        rows = []
        for p in positions:
            side = "LONG" if float(p.get("amount", 0)) > 0 else "SHORT"
            side_cls = "long" if side == "LONG" else "short"
            entry = float(p.get("entry_price", 0))
            mark = float(p.get("mark_price", entry))
            pnl_pct = ((mark / entry - 1) * 100 if side == "LONG" else (entry / mark - 1) * 100) if entry else 0.0
            pnl_cls = "pos" if pnl_pct >= 0 else "neg"
            rows.append(
                f"<tr><td>{esc(p.get('symbol',''))}</td>"
                f"<td><span class='side-chip {side_cls}'>{side}</span></td>"
                f"<td class='num'>{entry:.5g}</td><td class='num'>{mark:.5g}</td>"
                f"<td class='num {pnl_cls}'>{pnl_pct:+.2f}%</td></tr>"
            )
        positions_html = (
            "<table><thead><tr><th>심볼</th><th>방향</th><th>진입가</th><th>현재가</th><th>손익%</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
        )
    else:
        positions_html = "<div class='empty'>보유 중인 포지션 없음</div>"

    if recent:
        rows = []
        for r in recent:
            side = r.get("side", "")
            side_cls = "long" if side == "LONG" else "short"
            pnl = r.get("estimated_pnl_usdt") or 0
            pnl_cls = "pos" if pnl >= 0 else "neg"
            exited_at = r.get("exited_at", 0)
            t = datetime.fromtimestamp(exited_at, KST).strftime("%H:%M:%S") if exited_at else "—"
            reason = r.get("exit_reason", "—")
            rows.append(
                f"<tr><td class='mono'>{t}</td><td>{esc(r.get('symbol',''))}</td>"
                f"<td><span class='side-chip {side_cls}'>{side}</span></td>"
                f"<td class='num {pnl_cls}'>{fmt_usdt(pnl)}</td><td>{esc(reason)}</td></tr>"
            )
        recent_html = (
            "<table><thead><tr><th>시각</th><th>심볼</th><th>방향</th><th>손익</th><th>사유</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
        )
    else:
        recent_html = "<div class='empty'>거래 기록 없음</div>"

    return PAGE_TEMPLATE.format(
        hb_class=hb_class, hb_label=hb_label,
        updated_at=datetime.now(KST).strftime("%m-%d %H:%M:%S"),
        balance=balance_str,
        today_count=today["count"], today_wr=today["win_rate"],
        today_net=fmt_usdt(today["net"]), today_cls=("pos" if today["net"] >= 0 else "neg"),
        rot_count=rot["count"], rot_wr=rot["win_rate"],
        rot_net=fmt_usdt(rot["net"]), rot_cls=("pos" if rot["net"] >= 0 else "neg"),
        positions_html=positions_html,
        recent_html=recent_html, recent_count=len(recent),
        daily_loss_limit=f"{cfg.daily_loss_limit_pct:.0f}%" if cfg.daily_loss_limit_pct < 999 else "비활성(무제한)",
        loss_streak_limit=f"{cfg.global_loss_streak_threshold}연패" if cfg.global_loss_streak_threshold < 999 else "비활성(무제한)",
        position_size=f"{cfg.position_size_min*100:.0f}~{cfg.position_size_max*100:.0f}%",
        max_positions=f"{cfg.max_positions_high}개",
    )


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # 조용히(콘솔 스팸 방지)

    def do_GET(self):
        if self.path not in ("/", "/index.html"):
            self.send_response(404)
            self.end_headers()
            return
        try:
            html = render()
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"대시보드 렌더링 오류: {e}".encode("utf-8"))


if __name__ == "__main__":
    # [주의] 콘솔 코드페이지(cp949)가 이모지/특수문자를 못 그리면 print 자체가 예외를 던져
    # 서버가 바인딩되기도 전에 죽을 수 있다 — ASCII만 사용.
    print(f"dashboard server starting on http://127.0.0.1:{PORT}")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.serve_forever()
