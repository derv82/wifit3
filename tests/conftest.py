"""Global test fixtures.

``Campaign.active`` is a process-wide, class-level radio mutex: a test that starts
a real ``Campaign`` (``run()``) and doesn't tear it down leaves it set, which under
pytest-randomly's cross-module ordering poisons unrelated tests — e.g. the Focus
footer/button state is derived from it, so a leak greys/empties it. Reset it around
every test so no single test's leak can cascade. (``test_campaign_base`` keeps its
own local copy of this for when it runs in isolation.)
"""
import pytest

from wifit3.engine.attacks.campaign import Campaign


@pytest.fixture(autouse=True)
def _reset_campaign_active():
    Campaign.active = None
    yield
    Campaign.active = None
