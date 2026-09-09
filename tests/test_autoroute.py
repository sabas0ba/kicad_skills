import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import autoroute  # noqa: E402


@pytest.fixture
def fenced():
    """A board with one rectangle in the middle, open to a single net."""
    router = autoroute.Router(20.0, 20.0)
    router.add(autoroute.Obstacle(8.0, 8.0, 12.0, 12.0, "", None, open_to=frozenset({"OWN"})))
    return router


def _covers(router, net, point):
    cell = (int(point[0] / router.pitch), int(point[1] / router.pitch))
    return cell in router._blocked(net, 0.3)["F.Cu"]


def test_a_body_keepout_blocks_a_net_it_is_not_open_to(fenced):
    assert _covers(fenced, "OTHER", (10.0, 10.0))


def test_a_body_keepout_lets_the_parts_own_net_through(fenced):
    assert not _covers(fenced, "OWN", (10.0, 10.0))


def test_an_obstacle_with_no_exemptions_blocks_every_net():
    router = autoroute.Router(20.0, 20.0)
    router.add(autoroute.Obstacle(8.0, 8.0, 12.0, 12.0, "", None))

    assert _covers(router, "OWN", (10.0, 10.0))
    assert _covers(router, "OTHER", (10.0, 10.0))
