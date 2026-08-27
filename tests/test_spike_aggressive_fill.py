"""[2026-08-16] early_entry_spike 후보를 execute_entry()가 실제로 더 빠르게(스프레드 교차)
체결시키도록 연결하는 배선(wiring) 검증. place_entry_order() 자체의 aggressive=True 동작은
tests/test_limit_entry.py에서 이미 단위테스트로 검증했으므로, 여기서는 execute_entry가
candidate["early_entry_spike"]를 place_entry_order 호출에 정확히 전달하는지만 확인한다.
과도한 모킹으로 execute_entry 전체를 재현하기보다, 소스 스캔 + 좁은 범위 함수 단위테스트를
조합한다(이 프로젝트의 기존 test_defense_stack_floor.py 방식과 동일)."""
import re
import unittest
from pathlib import Path


class ExecuteEntryAggressiveWiringTests(unittest.TestCase):
    def test_execute_entry_passes_early_entry_spike_as_aggressive(self):
        src = Path("bot/main.py").read_text(encoding="utf-8")
        # execute_entry 안에서 candidate 기반 호출부를 찾는다(place_entry_order의 다른 호출부 제외).
        # [2026-08-25] 게이트 복원 — 스파이크 태그는 cfg.spike_entry_aggressive_fill과
        # AND된 뒤에야 fast_entry_lane에 들어간다(이 AND 유실이 실제 라이브 버그였다).
        call_idx = src.index('spike_aggressive = bool(candidate.get("early_entry_spike")) and cfg.spike_entry_aggressive_fill')
        snippet = src[call_idx:call_idx + 500]
        self.assertIn("fast_entry_lane", snippet)
        self.assertIn("aggressive=fast_entry_lane", snippet)
        self.assertIn('candidate.get("early_entry_spike"', snippet)

    def test_spike_enabled_false_means_flag_always_false(self):
        """cfg.spike_entry_enabled=false(기본값)면 scan_entry_candidate가 만드는
        early_entry_spike는 항상 False로 고정된다 — 즉 aggressive 분기 자체가
        기본 설정에서는 절대 실행되지 않는다(회귀 없음)."""
        src = Path("bot/main.py").read_text(encoding="utf-8")
        idx = src.index('early_entry_spike = bool(')
        snippet = src[idx:idx + 200]
        self.assertIn("cfg.spike_entry_enabled", snippet)

    def test_place_entry_order_aggressive_default_is_false(self):
        """place_entry_order 시그니처의 aggressive 기본값이 False인지 — 이 파일 밖의 다른
        모든 호출부(물타기 등)가 명시적으로 넘기지 않는 한 기존 동작 그대로 유지됨을 보장."""
        src = Path("bot/main.py").read_text(encoding="utf-8")
        idx = src.index("def place_entry_order(")
        snippet = src[idx:idx + 300]
        self.assertIn("aggressive: bool = False", snippet)


if __name__ == "__main__":
    unittest.main()
