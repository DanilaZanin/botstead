"""risk.browser_labels: все метки, под которые подходит описание действия, а не первая."""
import pytest

from bothub.risk import browser_labels, classify

pytestmark = pytest.mark.pure
TOOL = "mcp__bothub__browser"


def test_every_matching_label_is_listed_strict_ones_first_as_in_classify():
    assert browser_labels({"action": "click", "element": "Sign in and pay"}) == ["login", "pay"]
    assert browser_labels({"action": "click", "element": "Delete and send"}) == ["delete", "send"]
    assert browser_labels({"action": "fill", "element": "Card number"}) == ["pay"]
    assert browser_labels({"action": "click", "element": "Next"}) == []


def test_send_is_for_click_only_and_reads_are_not_labelled():
    assert browser_labels({"action": "fill", "element": "Send"}) == []
    assert browser_labels({"action": "snapshot"}) == []
    assert browser_labels({"action": "navigate", "url": "https://a.example/"}) == []


@pytest.mark.parametrize("args", [
    {"action": "click", "element": "Pay now"}, {"action": "fill", "target": "#password"},
    {"action": "click", "page_label": "button Delete"}, {"action": "click", "element": "Submit"},
])
def test_the_first_label_is_what_classify_returns(args):
    assert classify(TOOL, args) == browser_labels(args)[0]
