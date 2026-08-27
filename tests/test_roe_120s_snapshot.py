"""[2026-08-19] 진입 후 120초 시점 ROE 스냅샷 — S2(조건부 시간컷) 검증용 관측 필드.

**왜 120초인가**
V2 재배포 이후 629건을 보유시간으로 쪼개면 2~5분 구간만 유독 나쁘다:
    ~2분    163건(25.9%) 승률 96.9%  명목당 net +0.6307%  net합 +10.018
    2~5분   229건(36.4%) 승률 48.5%  명목당 net -0.3818%  net합  -8.767
    5~15분  202건(32.1%) 승률 64.4%  명목당 net -0.1724%  net합  -3.385
    15분~    35건( 5.6%) 승률 54.3%  명목당 net -0.3924%  net합  -1.217
그 나쁜 구간의 시작점이 120초다. 기존 스냅샷은 30초/60초뿐이라 이 지점을 못 본다.

**기각 이력과의 차이 — 반드시 구분할 것**
1) 2026-08-17: "무조건 120/180초 후 컷" -> 승률 -11~12%p로 기각.
2) 2026-08-19: 30초/60초 ROE로 불량(고점<1.5%) 판별 -> 사전등록 기준(탐지>=60% AND
   오탐<=20%) 미달로 정식 기각.
이번에 재려는 것은 둘 다와 다르다. **"해당 시점에 아직 무장하지 못한 거래"만** 대상으로
하는 조건부이며, 무장 거래(승률 98.8%, 명목당 net +1.0566%)는 절대 자르지 않는다.
사전 역시뮬(416건, 60초 대리지표 포함)에서 walk-forward 양쪽 창 개선 +1.99/+0.59가 나와
측정할 가치가 확인됐으나, 그 표본의 절반이 1분봉 종가 복원값(일치율 79~89%)이라
**실측으로 재확인이 필요**해서 이 필드를 넣는다.

**이 값은 청산 판단에 일절 쓰지 않는다.** 측정 먼저, 규칙은 그다음.
판정 기준(사전 고정): 실측 표본 150건 이상에서 walk-forward 양쪽 창 순익 개선 > 0 AND
무장 거래 컷 0건 AND 일자별 플러스 2/3 이상. 하나라도 미달이면 방향을 닫는다.

이 테스트가 지키는 것:
1. 스냅샷이 청산 판단을 바꾸지 않는다
2. 120초 경과 후 첫 폴링에서 한 번만 기록되고 이후 덮어쓰이지 않는다
3. 그 전에는 None으로 남아 "아직 안 지남"과 "ROE 0"이 구분된다
4. 평단가가 바뀌면(물타기) 기준점과 함께 초기화된다
5. 원장까지 실제로 전달된다
"""
import ast
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.config import Config
from bot.position_manager import PositionManager, TrackedPosition
from bot.trade_ledger import TradeRecord


def _pm(entered_at, entry=100.0, side="LONG"):
    pm = PositionManager(Config())
    pm.positions["TESTUSDT"] = TrackedPosition(
        symbol="TESTUSDT", side=side, entry_price=entry, quantity=1.0,
        leverage=4.0, entered_at=entered_at,
    )
    return pm


class Snapshot120sTimingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._p = patch("bot.position_manager.STATS_FILE", Path(self._tmp.name) / "s.json")
        self._p.start()

    def tearDown(self):
        self._p.stop()
        self._tmp.cleanup()

    def test_none_before_120s(self):
        """60초는 찍혔는데 120초는 아직 None이어야 한다."""
        now = time.time()
        pm = _pm(now)
        with patch("bot.position_manager.time.time", return_value=now + 61):
            pm.evaluate("TESTUSDT", 100.0)
        pos = pm.positions["TESTUSDT"]
        self.assertIsNotNone(pos.roe_at_60s)
        self.assertIsNone(pos.roe_at_120s, "120초 전에는 None이어야 한다")

    def test_recorded_after_120s(self):
        now = time.time()
        pm = _pm(now)
        with patch("bot.position_manager.time.time", return_value=now + 121):
            pm.evaluate("TESTUSDT", 100.0 * (1 - 0.025 / 4))  # ROE -2.5%
        self.assertAlmostEqual(pm.positions["TESTUSDT"].roe_at_120s, -2.5, places=3)

    def test_not_overwritten_later(self):
        """시점 스냅샷이므로 이후 폴링이 덮어쓰면 안 된다."""
        now = time.time()
        pm = _pm(now)
        with patch("bot.position_manager.time.time", return_value=now + 121):
            pm.evaluate("TESTUSDT", 100.0 * (1 - 0.025 / 4))
        first = pm.positions["TESTUSDT"].roe_at_120s
        with patch("bot.position_manager.time.time", return_value=now + 300):
            pm.evaluate("TESTUSDT", 100.0 * (1 + 0.08 / 4))
        self.assertEqual(pm.positions["TESTUSDT"].roe_at_120s, first)

    def test_all_three_when_first_poll_is_late(self):
        """첫 폴링이 120초 뒤면 30/60/120이 같은 관측치로 채워진다(폴링 주기 약 5초라 정상)."""
        now = time.time()
        pm = _pm(now)
        with patch("bot.position_manager.time.time", return_value=now + 130):
            pm.evaluate("TESTUSDT", 100.0 * (1 + 0.04 / 4))
        pos = pm.positions["TESTUSDT"]
        for f in ("roe_at_30s", "roe_at_60s", "roe_at_120s"):
            self.assertAlmostEqual(getattr(pos, f), 4.0, places=3, msg=f)

    def test_short_side_sign(self):
        now = time.time()
        pm = _pm(now, side="SHORT")
        with patch("bot.position_manager.time.time", return_value=now + 121):
            pm.evaluate("TESTUSDT", 100.0 * (1 - 0.03 / 4))  # 숏은 하락이 유리
        self.assertAlmostEqual(pm.positions["TESTUSDT"].roe_at_120s, 3.0, places=3)

    def test_verdict_unchanged(self):
        """관측 코드가 청산 판단을 바꾸면 안 된다 — 스냅샷 유무와 무관하게 같은 결론."""
        now = time.time()
        for elapsed in (10, 121):
            with self.subTest(elapsed=elapsed):
                pm = _pm(now)
                with patch("bot.position_manager.time.time", return_value=now + elapsed):
                    verdict = pm.evaluate("TESTUSDT", 100.0 * (1 + 0.001 / 4))
                self.assertIsNone(verdict, "미미한 변동에서 청산 판단이 나오면 안 된다")


class ResetAndLedgerTests(unittest.TestCase):
    def test_reset_on_average_down(self):
        """평단가가 바뀌면 옛 기준의 스냅샷이 남아 오판을 만들면 안 된다."""
        src = Path("bot/position_manager.py").read_text(encoding="utf-8-sig")
        idx = src.find("pos.roe_at_30s = None\n        pos.roe_at_60s = None")
        self.assertGreater(idx, 0, "물타기 초기화 블록을 찾지 못했다")
        block = src[idx:idx + 200]
        self.assertIn("pos.roe_at_120s = None", block,
                      "물타기 시 roe_at_120s도 함께 초기화돼야 한다")

    def test_trade_record_default_none(self):
        self.assertIsNone(TradeRecord.__dataclass_fields__["roe_at_120s"].default)

    def test_main_passes_snapshot_to_ledger(self):
        """원장 기록부가 roe_at_120s를 실제로 넘기는지 소스로 확인한다."""
        src = Path("bot/main.py").read_text(encoding="utf-8-sig")
        self.assertIn('roe_at_120s=getattr(pos, "roe_at_120s", None)', src)
        tree = ast.parse(src)
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "TradeRecord":
                kws = {k.arg for k in node.keywords}
                if "roe_at_60s" in kws:
                    found = True
                    self.assertIn("roe_at_120s", kws,
                                  "roe_at_60s를 넘기는 TradeRecord 생성부가 120s를 빠뜨렸다")
        self.assertTrue(found, "TradeRecord 생성부를 찾지 못했다")

    def test_reset_on_entry_price_change_in_main(self):
        """main.py의 평단가 변경 경로에서도 초기화되는지."""
        src = Path("bot/main.py").read_text(encoding="utf-8-sig")
        idx = src.find("pos.roe_at_30s = None\n                    pos.roe_at_60s = None")
        self.assertGreater(idx, 0, "main.py 평단가 변경 초기화 블록을 찾지 못했다")
        self.assertIn("pos.roe_at_120s = None", src[idx:idx + 250])


if __name__ == "__main__":
    unittest.main()
