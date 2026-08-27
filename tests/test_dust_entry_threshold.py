"""[2026-08-25 버그수정] 먼지 진입 판정 임계 테스트.

버그: 임계가 "진입 목표하한 x 0.5"였다. 하한이 2.5였던 시절엔 1.25라 무해했는데,
사용자 요청으로 하한을 10 USDT로 올리자 임계가 5.0이 되어 4 USDT짜리 정상 부분체결이
즉시 시장가로 되팔렸다(수수료+스프레드만 물고 거래는 0건 처리).
먼지 판정은 "관리 불가능할 만큼 작은 체결"을 걷어내는 것이지 "목표보다 작은 체결"을
걷어내는 게 아니므로, 진입 하한과 분리해 절대값으로 둔다.
"""
import unittest

from bot.config import Config
from bot.main import _is_effectively_dust_entry, dust_entry_threshold_usdt


def _cfg(dust=1.25):
    cfg = Config()
    cfg.dust_entry_max_margin_usdt = dust
    return cfg


class DustEntryThresholdTests(unittest.TestCase):
    def test_threshold_is_independent_of_entry_floor(self):
        """진입 하한을 2.5 -> 10으로 올려도 먼지 임계는 안 따라 올라가야 한다."""
        cfg = _cfg()
        self.assertEqual(dust_entry_threshold_usdt(2.5, cfg), 1.25)
        self.assertEqual(dust_entry_threshold_usdt(10.0, cfg), 1.25)

    def test_regression_four_usdt_partial_fill_is_kept(self):
        """이 버그의 실제 증상 — 하한 10 USDT 환경에서 4 USDT 부분체결이 살아남아야 한다."""
        self.assertFalse(_is_effectively_dust_entry(4.0, 10.0, _cfg()))
        # 수정 전 동작(비례 방식)에서는 정리 대상이었다는 것도 함께 고정한다.
        self.assertTrue(_is_effectively_dust_entry(4.0, 10.0, None))

    def test_true_dust_is_still_cleaned(self):
        """정말 관리 불가능한 체결(레버 4배 기준 명목 5 USDT 미만)은 여전히 정리한다."""
        self.assertTrue(_is_effectively_dust_entry(0.5, 10.0, _cfg()))
        self.assertTrue(_is_effectively_dust_entry(1.0, 10.0, _cfg()))

    def test_boundary_is_not_dust(self):
        """임계와 같은 값은 정리 대상이 아니다(부동소수 오차 포함)."""
        self.assertFalse(_is_effectively_dust_entry(1.25, 10.0, _cfg()))

    def test_zero_config_falls_back_to_legacy(self):
        """0으로 두면 예전 비례 방식으로 되돌아간다(원복 경로 보존)."""
        self.assertEqual(dust_entry_threshold_usdt(10.0, _cfg(dust=0.0)), 5.0)

    def test_code_default_is_absolute(self):
        self.assertEqual(Config().dust_entry_max_margin_usdt, 1.25)


if __name__ == "__main__":
    unittest.main()
