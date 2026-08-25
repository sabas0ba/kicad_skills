import itertools
import math

from eda_toolkit.kicad import outline


def rect(x0, y0, x1, y1):
    return {"type": "gr_rect", "start": (x0, y0), "end": (x1, y1)}


def circle(cx, cy, r):
    return {"type": "gr_circle", "centre": (cx, cy), "radius": r}


def test_rectangle_flattens_to_four_closed_sides():
    segments = outline.flatten([rect(0, 0, 50, 40)])
    assert len(segments) == 4
    assert outline.is_closed(segments)
    assert outline.bbox(segments) == (0, 0, 50, 40)


def test_circle_bbox_is_the_circle_not_its_centre_and_rim():
    segments = outline.flatten([circle(10, 10, 5)])
    min_x, min_y, max_x, max_y = outline.bbox(segments)
    assert math.isclose(min_x, 5, abs_tol=0.02)
    assert math.isclose(max_x, 15, abs_tol=0.02)
    assert math.isclose(min_y, 5, abs_tol=0.02)
    assert math.isclose(max_y, 15, abs_tol=0.02)
    assert outline.is_closed(segments)


def test_arc_bulges_away_from_its_chord():
    # Half circle from (0,0) to (10,0) bulging through (5,5).
    segments = outline.flatten([{"type": "gr_arc", "start": (0, 0), "mid": (5, 5), "end": (10, 0)}])
    _, _, _, max_y = outline.bbox(segments)
    assert math.isclose(max_y, 5, abs_tol=0.05)
    # The chord midpoint is 5 mm from the arc, not on it.
    assert math.isclose(outline.distance((5, 0), segments), 5, abs_tol=0.05)


def test_arc_takes_the_short_way_when_the_midpoint_says_so():
    segments = outline.flatten(
        [{"type": "gr_arc", "start": (0, 0), "mid": (5, -5), "end": (10, 0)}]
    )
    _, min_y, _, max_y = outline.bbox(segments)
    assert math.isclose(min_y, -5, abs_tol=0.05)
    assert math.isclose(max_y, 0, abs_tol=0.05)


def test_collinear_arc_degrades_to_a_line():
    segments = outline.flatten([{"type": "gr_arc", "start": (0, 0), "mid": (5, 0), "end": (10, 0)}])
    assert segments == [((0, 0), (10, 0))]


def test_distance_is_measured_to_the_nearest_edge():
    segments = outline.flatten([rect(0, 0, 50, 40)])
    assert math.isclose(outline.distance((5, 20), segments), 5)
    assert math.isclose(outline.distance((25, 38), segments), 2)


def test_inside_outside_and_cutouts():
    board = outline.flatten([rect(0, 0, 50, 40), circle(25, 20, 5)])
    assert outline.contains((10, 10), board)
    assert not outline.contains((60, 10), board)
    # Inside the mounting-hole cutout is not inside the board.
    assert not outline.contains((25, 20), board)


def test_clearance_is_signed():
    segments = outline.flatten([rect(0, 0, 50, 40)])
    assert outline.clearance((5, 20), segments) > 0
    assert outline.clearance((-5, 20), segments) < 0


def test_bounding_box_would_miss_a_cutout():
    """The point of the exercise: a bbox check calls this copper safe."""
    board = outline.flatten([rect(0, 0, 50, 40), circle(25, 20, 5)])
    point = (25, 25.2)  # 0.2 mm from the cutout, 15 mm from every bbox side
    assert math.isclose(outline.clearance(point, board), 0.2, abs_tol=0.02)
    min_x, min_y, max_x, max_y = outline.bbox(board)
    bbox_margin = min(point[0] - min_x, max_x - point[0], point[1] - min_y, max_y - point[1])
    assert bbox_margin > 14


def test_open_outline_is_not_closed():
    segments = outline.flatten(
        [
            {"type": "gr_line", "start": (0, 0), "end": (10, 0)},
            {"type": "gr_line", "start": (10, 0), "end": (10, 10)},
        ]
    )
    assert not outline.is_closed(segments)


def test_polyline_edges_are_supported():
    segments = outline.flatten(
        [{"type": "gr_poly", "polyline": [(0, 0), (10, 0), (10, 10), (0, 10)]}]
    )
    assert len(segments) == 4
    assert outline.is_closed(segments)


def test_a_shape_made_of_two_outlines_sharing_a_seam():
    """A USB-stick board: a body plus a tab, drawn as two outlines that touch.

    The seam is a horizontal line at the join, so a ray cast along it runs
    through vertices and collinear edges - and used to report the middle of the
    board as outside it.
    """
    body = outline.flatten(
        [
            {"type": "gr_line", "start": (0, 0), "end": (20, 0)},
            {"type": "gr_line", "start": (20, 0), "end": (20, 10)},
            {"type": "gr_line", "start": (20, 10), "end": (15, 10)},  # right of the seam
            {"type": "gr_line", "start": (5, 10), "end": (0, 10)},  # left of the seam
            {"type": "gr_line", "start": (0, 10), "end": (0, 0)},
        ]
    )
    tab = outline.flatten(
        [
            {"type": "gr_line", "start": (5, 10), "end": (5, 16)},
            {"type": "gr_line", "start": (5, 16), "end": (15, 16)},
            {"type": "gr_line", "start": (15, 16), "end": (15, 10)},
        ]
    )
    board = body + tab
    assert outline.contains((10, 5), board)  # in the body
    assert outline.contains((10, 13), board)  # in the tab
    assert outline.contains((10, 10), board)  # exactly on the seam
    assert not outline.contains((10, 20), board)  # past the tab
    assert not outline.contains((-1, 5), board)


def test_a_large_arc_is_tessellated_to_its_tolerance():
    """A 100 mm radius at 24 fixed steps sagged 0.2 mm - twice the edge rule."""
    import math

    pts = outline.circle_points((0.0, 0.0), 100.0)
    worst = 100.0
    for a, b in itertools.pairwise(pts):
        mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        worst = min(worst, math.hypot(*mid))
    assert 100.0 - worst < outline.CHORD_TOLERANCE_MM * 1.5


def test_a_bezier_edge_follows_the_curve_not_the_control_cage():
    """gr_curve stores controls the copper never visits; the flatten must too."""
    edge = {
        "type": "gr_curve",
        "polyline": [(0.0, 0.0), (0.0, 10.0), (10.0, 10.0), (10.0, 0.0)],
    }
    segments = outline.flatten([edge])
    ys = [p[1] for seg in segments for p in seg]
    # the cubic's apex is 7.5; joining the cage as vertices would reach 10
    assert max(ys) < 8.0
    assert segments[0][0] == (0.0, 0.0)
    assert segments[-1][1] == (10.0, 0.0)
