"""Tests for interview note material. Run: python3 tests/test_interviewnotes.py"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import interviewnotes  # noqa: E402


def test_a_story_is_kept_and_visa_or_pay_lines_are_dropped():
    note = ("# Interview Acme\n\n## Checkout story\n\n"
            "I rebuilt the checkout one question at a time. Checkout completion "
            "went from 20% to 45% and the team kept growing after launch, with "
            "time to finish near five minutes for most people who started.\n"
            "My visa status is fine and I need no sponsorship.\n"
            "I am asking for $180k base.\n")
    text = " ".join(body for _, body in interviewnotes.sections("Interview Acme", note))
    assert "20% to 45%" in text, text
    assert "sponsorship" not in text and "$180k" not in text, text


if __name__ == "__main__":
    test_a_story_is_kept_and_visa_or_pay_lines_are_dropped()
    print("1/1 passed")
