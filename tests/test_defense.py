"""The optional defense filter: off by default, and when it is on, defense
roles never show up or alert."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jobs.common import is_defense

ON = {"skip_defense": True}
OFF = {}


def test_the_filter_is_off_by_default():
    assert not is_defense("Anduril Industries", "Senior Product Designer", cfg=OFF)


def test_defense_title_is_out():
    assert is_defense("Example Drones", "UX Product Designer, National Security", cfg=ON)
    assert is_defense("Applied Intuition, Inc.", "Product Designer, Defense", cfg=ON)


def test_defense_company_is_out():
    assert is_defense("Anduril Industries", "Senior Product Designer", cfg=ON)
    assert is_defense("Palantir Technologies", "Product Designer", cfg=ON)


def test_civic_and_government_design_is_fine():
    assert not is_defense("Code for America", "Product Designer, Government Services", cfg=ON)


def test_clearance_in_posting_is_out():
    assert is_defense("Acme", "Product Designer", "Candidates must hold an active security clearance.", cfg=ON)
    assert is_defense("Acme", "Product Designer", "This role is subject to ITAR.", cfg=ON)


def test_equal_opportunity_line_is_not_defense():
    eeo = "We do not discriminate based on age, military or veteran status, or disability."
    assert not is_defense("Calm.com", "Senior Product Designer", eeo, cfg=ON)


def test_company_serving_many_markets_is_not_defense():
    text = "We serve the automotive, defense, trucking and mining industries."
    assert not is_defense("Applied Intuition, Inc.", "Product Designer, HMI", text, cfg=ON)
