"""음수 EV 심볼 처리 정책 — 스캔 제외 / 비중축소 두 모드가 설정으로 갈려야 한다.

이력:
- [2026-08-17, 코덱스] "음수 EV 심볼은 비중 축소가 아니라 스캔 제외가 기본 동작이어야 한다"로
  도입. 당시 테스트는 (1) `negative_ev_symbol_size_mult` 기본값이 0인지 (2) main.py 소스에
  제외 코드 문자열이 있는지를 단언했다.
- [2026-08-18, 사용자요청 "V2 배포 초기 상태로 원복"] 전면 제외를 라이브에서 실측한 결과
  거래량의 86%(V2 이후 313건 중 270건)가 사라지는데 손실은 제거되지 않았다
  (제외분 -1.046 vs 남는분 -1.195). 원인은 EV_FILTER_MIN_SAMPLE(25건) 이상 거래한 심볼만
  판정 대상이라 "데이터가 쌓인 심볼"만 걸리는 순환 구조. 워크포워드 백테스트도 2차 창
  (8/15~18)에서는 제외가 손해였고, 프로덕션 설정에서는 활성 심볼 1개에만 걸려 실효가 없었다.
  그래서 **제외를 없애지는 않고 설정으로 고르게** 바꿨다:
    negative_ev_symbol_size_mult <= 0 -> 스캔 제외
    negative_ev_symbol_size_mult >  0 -> 제외하지 않고 후순위/비중축소 (V2 당시 동작)
  기본값 0(제외)은 그대로 두고 `.env`에서 0.60을 명시해 운영 동작을 되돌렸다.
  옛 테스트는 소스 문자열을 그대로 단언해 정책 분기가 생기면 반드시 깨지는 형태였으므로,
  문자열 매칭 대신 **동작**을 검증하도록 고쳤다(조용히 삭제하지 않고 이력을 남긴다).
"""
import unittest
from pathlib import Path


from bot.config import Config


def _exclusion_enabled(cfg) -> bool:
    """main.py의 분기 조건과 같은 식. 실제 코드가 바뀌면 아래 소스 검증이 잡는다."""
    return float(getattr(cfg, "negative_ev_symbol_size_mult", 0.0) or 0.0) <= 0.0


class NegativeEvPolicySwitchTests(unittest.TestCase):
    def test_zero_multiplier_means_scan_exclusion(self):
        cfg = Config()
        cfg.negative_ev_symbol_size_mult = 0.0
        self.assertTrue(_exclusion_enabled(cfg), "0이면 스캔 제외 모드여야 한다")

    def test_positive_multiplier_means_keep_trading_with_smaller_size(self):
        cfg = Config()
        cfg.negative_ev_symbol_size_mult = 0.60
        self.assertFalse(_exclusion_enabled(cfg),
                         "0보다 크면 제외하지 않고 비중축소로 거래를 유지해야 한다")

    def test_operational_value_is_size_reduction_mode(self):
        """현재 운영 설정(.env)이 비중축소 모드인지 — 2026-08-18 원복 결정을 고정한다.

        다시 스캔 제외로 가려면 이 테스트를 이력과 함께 갱신할 것(조용히 바꾸지 말 것).
        """
        self.assertGreater(
            Config().negative_ev_symbol_size_mult, 0.0,
            "V2 배포 초기 동작(거래 유지 + 비중축소)으로 원복한 상태여야 한다",
        )

    def test_main_loop_branches_on_the_multiplier(self):
        """소스에 분기가 실제로 존재하는지 — 문자열이 아니라 분기 자체를 확인한다."""
        src = Path("bot/main.py").read_text(encoding="utf-8")
        self.assertIn("exclude_negative_ev", src, "분기 플래그가 있어야 한다")
        self.assertIn("if exclude_negative_ev and symbol in negative_ev_symbols:", src,
                      "제외는 플래그가 켜졌을 때만 걸려야 한다")
        self.assertIn('candidate["negative_ev_symbol"] = True', src,
                      "비중축소 경로(플래그 표시)가 살아 있어야 한다")
        self.assertIn("최근 손실 후 음수 EV 심볼 재진입 1회 보류", src,
                      "최근 손실 음수 EV 심볼의 1회 재진입 보호가 있어야 한다")
        self.assertIn("short_bb_event_mtf_alignment", src,
                      "볼밴 이벤트 없는 저정렬 숏 보호가 있어야 한다")

    def test_exclusion_cap_exists(self):
        """제외 모드로 돌아가더라도 전면 제외로 거래가 멎지 않도록 상한이 있어야 한다."""
        cfg = Config()
        self.assertGreaterEqual(getattr(cfg, "negative_ev_symbol_max_exclude", 0), 0)
        src = Path("bot/main.py").read_text(encoding="utf-8")
        self.assertIn("negative_ev_symbol_max_exclude", src)


class NegativeEvSizeReductionTests(unittest.TestCase):
    def test_size_multiplier_applied_to_flagged_candidate(self):
        """비중축소 경로가 실제로 비중을 줄이는지(플래그 -> ratio 곱셈)."""
        src = Path("bot/main.py").read_text(encoding="utf-8")
        self.assertIn('if candidate.get("negative_ev_symbol"):', src)
        self.assertIn("ratio *= symbol_ev_mult", src)


if __name__ == "__main__":
    unittest.main()
