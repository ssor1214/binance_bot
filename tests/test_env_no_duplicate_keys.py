"""[2026-08-17] .env에 같은 키가 두 번 정의되는 것을 막는다.

실제로 두 건이 있었다:
  AGGRESSIVE_MIN_SIGNAL_CONFIRMATIONS  80행=2  / 399행=1  (실효값 1)
  SPIKE_ENTRY_ENABLED                 752행=false / 810행=true (실효값 true)

dotenv는 나중 값이 이기므로 앞쪽 줄은 조용히 무시된다. 앞쪽만 보고 "꺼져 있다"고
판단하거나 앞쪽만 고치고 반영됐다고 착각하기 쉬운 함정이라, 테스트로 재발을 막는다.
(실제로 8/16 롤백 때 752행을 false로 바꿨는데 810행 때문에 계속 켜져 있었다.)
"""
import unittest
from collections import Counter
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def _defined_keys(path: Path) -> list[str]:
    """주석/빈 줄을 제외하고 실제로 정의된 키만 뽑는다."""
    keys = []
    if not path.exists():
        return keys
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key:
            keys.append(key)
    return keys


class EnvDuplicateKeyTests(unittest.TestCase):
    def test_no_duplicate_keys(self):
        dups = [k for k, n in Counter(_defined_keys(ENV_PATH)).items() if n > 1]
        self.assertEqual(
            dups, [],
            "\n.env에 중복 정의된 키가 있습니다: %s\n"
            "dotenv는 나중 값이 이기므로 앞쪽 줄은 무시됩니다.\n"
            "앞쪽 줄을 주석 처리하고 '어느 줄이 유효한지' 사유 주석을 남기세요." % dups,
        )

    def test_commented_out_duplicates_are_documented(self):
        """정리한 흔적(주석)이 남아 있어야 다음 사람이 왜 주석인지 알 수 있다."""
        text = ENV_PATH.read_text(encoding="utf-8")
        self.assertIn("중복키 정리", text)


if __name__ == "__main__":
    unittest.main()
