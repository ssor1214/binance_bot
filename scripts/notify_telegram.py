"""[2026-08-15 사용자요청] "이것들을 나는 텔레그램에서 보고받고 싶은데" — 매시 모니터링
브리핑/에이전트 작업 결과를 라이브 봇 프로세스와 무관하게 텔레그램으로 즉시 전송하기 위한
독립 실행 스크립트. 라이브 bot.main 프로세스를 전혀 건드리지 않는다(같은 봇 토큰으로
sendMessage만 호출 — getUpdates 폴링이 아니므로 라이브 쪽 폴링 루프와 충돌 없음).

사용법:
    python scripts/notify_telegram.py "메시지 내용"
    echo "메시지 내용" | python scripts/notify_telegram.py

긴 메시지는 텔레그램 4096자 제한에 맞춰 자동으로 잘라 여러 통으로 나눠 보낸다.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import Config
from bot.telegram_notifier import TelegramNotifier

TELEGRAM_MAX_LEN = 4000  # 4096자 제한에 여유를 둠


def main():
    if len(sys.argv) > 1:
        text = " ".join(sys.argv[1:])
    else:
        text = sys.stdin.read()
    text = text.strip()
    if not text:
        print("전송할 메시지가 비어있습니다.", file=sys.stderr)
        sys.exit(1)

    cfg = Config()
    tg = TelegramNotifier(cfg, ex=None, pm=None)
    if not tg.enabled:
        print("텔레그램 미설정(토큰/채팅ID 없음) — 전송 생략", file=sys.stderr)
        sys.exit(1)

    chunks = [text[i:i + TELEGRAM_MAX_LEN] for i in range(0, len(text), TELEGRAM_MAX_LEN)] or [text]
    for i, chunk in enumerate(chunks):
        prefix = f"[{i + 1}/{len(chunks)}]\n" if len(chunks) > 1 else ""
        tg.send(prefix + chunk)
    print(f"전송 완료 ({len(chunks)}통)")


if __name__ == "__main__":
    main()
