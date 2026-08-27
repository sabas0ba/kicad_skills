"""The thermal solve, held to conservation and to hand calculations.

The strongest reference is the one the physics gives for free: at steady state
every watt dissipated is a watt convected, whatever the geometry. The second is
the bath: a uniformly heated plate with adiabatic edges has no gradients, and
its rise is P / (h * A * 2) on a calculator. Everything else is comparative,
which is how the tool is meant to be read anyway.
"""

import math

import pytest

from eda_toolkit.kicad import pcb as pcb_mod
from eda_toolkit.kicad import thermal


class _Pad:
    def __init__(self, x, y, w=1.0, h=1.0, layers=("*.Cu",)):
        self.x, self.y, self.size = x, y, (w, h)
        self.layers = list(layers)
        self.net = ""
        self.drill = None

    def bbox(self, angle_offset=0.0, margin=0.0):
        return (
            self.x - self.size[0] / 2 - margin,
            self.y - self.size[1] / 2 - margin,
            self.x + self.size[0] / 2 + margin,
            self.y + self.size[1] / 2 + margin,
        )


class _Part:
    def __init__(self, ref, x, y, half=2.0, pads=(), courtyard=()):
        self.ref, self.x, self.y, self.angle = ref, x, y, 0.0
        self.half = half
        self.pads = list(pads)
        self.courtyard = list(courtyard)

    def courtyard_box(self):
        return (self.x - self.half, self.y - self.half, self.x + self.half, self.y + self.half)


class _Zone:
    def __init__(self, layer, points):
        self.keepout = False
        self.net = "GND"
        self.fills = [(layer, points)]


class _Board:
    """A rectangular board the model can be hand-checked on."""

    def __init__(
        self, width=40.0, height=30.0, footprints=(), zones=(), tracks=(), vias=(), closed=True
    ):
        self.width, self.height = width, height
        self.copper_layers = ["F.Cu", "B.Cu"]
        self.stackup = [
            {"name": "F.Cu", "type": "copper", "thickness": 0.035},
            {"name": "core", "type": "dielectric", "thickness": 1.6},
            {"name": "B.Cu", "type": "copper", "thickness": 0.035},
        ]
        self.footprints = list(footprints)
        self.zones = list(zones)
        self.tracks = list(tracks)
        self.vias = list(vias)
        self.closed = closed

    def outline_bbox(self):
        return (0.0, 0.0, self.width, self.height)

    def outline_closed(self):
        return self.closed

    def edge_clearance_at(self, x, y):
        return min(x, y, self.width - x, self.height - y)

    def footprint_by_ref(self, ref):
        return next((fp for fp in self.footprints if fp.ref == ref), None)


def test_every_watt_in_is_a_watt_convected_out():
    board = _Board(footprints=[_Part("U1", 10, 10)])
    result = thermal.analyse(board, {"U1": 1.5}, step_mm=1.0)
    assert result["balance"]["power_in_w"] == pytest.approx(1.5, abs=1e-6)
    assert result["balance"]["residual"] < 0.002


def test_a_uniformly_heated_plate_is_the_hand_calculation():
    """Cover the whole board with the source: no gradients, rise = P/(2hA)."""
    board = _Board(footprints=[_Part("HEAT", 20, 15, half=25.0)])
    watts = 1.0
    result = thermal.analyse(board, {"HEAT": watts}, htc_w_m2k=10.0, step_mm=1.0)
    area_m2 = 40e-3 * 30e-3
    by_hand = watts / (2 * 10.0 * area_m2)
    assert result["max_rise_c"] == pytest.approx(by_hand, rel=0.02)
    # no gradients: the part's rise and the board's maximum are the same number
    assert result["parts"][0]["rise_c"] == pytest.approx(result["max_rise_c"], rel=0.02)


def test_a_drilled_hole_is_not_board_material_either():
    """The bath again, minus the hole: rise = P/(2h(A - hole)) on a calculator."""
    hole = _Pad(20, 15, w=10.0, h=10.0)
    hole.type = "np_thru_hole"
    hole.drill = 10.0
    board = _Board(footprints=[_Part("HEAT", 20, 15, half=25.0), _Part("H1", 20, 15, pads=[hole])])
    result = thermal.analyse(board, {"HEAT": 1.0}, htc_w_m2k=10.0, step_mm=1.0)
    hole_m2 = math.pi * (5e-3) ** 2
    by_hand = 1.0 / (2 * 10.0 * (40e-3 * 30e-3 - hole_m2))
    assert result["max_rise_c"] == pytest.approx(by_hand, rel=0.03)
    assert result["balance"]["residual"] < 0.002


def test_a_pour_spreads_the_heat_the_bare_laminate_cannot():
    """The comparative question the tool exists for, answered both ways."""
    part = _Part("U1", 8, 8)
    bare = thermal.analyse(_Board(footprints=[part]), {"U1": 1.0}, step_mm=1.0)
    poured = thermal.analyse(
        _Board(
            footprints=[part],
            zones=[_Zone("F.Cu", [(0, 0), (40, 0), (40, 30), (0, 30)])],
        ),
        {"U1": 1.0},
        step_mm=1.0,
    )
    assert poured["max_rise_c"] < bare["max_rise_c"] * 0.5, (
        f"copper {poured['max_rise_c']} vs bare {bare['max_rise_c']}: the plane must spread"
    )
    assert poured["copper_coverage"] > 0.9
    assert bare["copper_coverage"] < 0.1


def test_copper_drawn_as_graphics_spreads_like_a_pour():
    """A heatsink patch drawn with gr_poly is copper, not a hole in the map."""
    part = _Part("U1", 8, 8)
    board = _Board(footprints=[part])
    board.copper_shapes = [("F.Cu", [(0, 0), (40, 0), (40, 30), (0, 30)])]
    bare = thermal.analyse(_Board(footprints=[part]), {"U1": 1.0}, step_mm=1.0)
    drawn = thermal.analyse(board, {"U1": 1.0}, step_mm=1.0)
    assert drawn["copper_coverage"] > 0.9
    assert drawn["max_rise_c"] < bare["max_rise_c"] * 0.5


def test_the_hot_spot_is_under_the_part_that_burns():
    board = _Board(footprints=[_Part("U1", 10, 10), _Part("R5", 30, 20)])
    result = thermal.analyse(board, {"U1": 2.0, "R5": 0.25}, step_mm=1.0)
    assert [p["ref"] for p in result["parts"]] == ["U1", "R5"]
    hx, hy = result["hotspot_mm"]
    assert math.dist((hx, hy), (10, 10)) < 4.0
    assert result["max_temperature_c"] == pytest.approx(
        result["ambient_c"] + result["max_rise_c"], abs=0.1
    )


def test_nonsense_is_refused_with_its_reason():
    board = _Board(footprints=[_Part("U1", 10, 10)])
    with pytest.raises(ValueError, match="at least one"):
        thermal.analyse(board, {})
    with pytest.raises(ValueError, match="no footprint"):
        thermal.analyse(board, {"U9": 1.0})
    with pytest.raises(ValueError, match="positive"):
        thermal.analyse(board, {"U1": -1.0})
    with pytest.raises(ValueError, match="positive"):
        thermal.analyse(board, {"U1": 1.0}, step_mm=0.0)
    with pytest.raises(ValueError, match="positive"):
        thermal.analyse(board, {"U1": 1.0}, htc_w_m2k=0.0)
    with pytest.raises(ValueError, match="finite"):
        thermal.analyse(board, {"U1": 1.0}, ambient_c=float("nan"))


def test_an_open_outline_is_refused_rather_than_filled_in():
    board = _Board(footprints=[_Part("U1", 10, 10)], closed=False)
    with pytest.raises(ValueError, match="not closed"):
        thermal.analyse(board, {"U1": 1.0}, step_mm=1.0)


def test_a_non_plated_hole_is_not_copper():
    """A big NPTH mounting hole must not become a fictitious conductive disc."""
    hole = _Pad(20, 15, w=6.0, h=6.0)
    hole.type = "np_thru_hole"
    board = _Board(footprints=[_Part("H1", 20, 15, pads=[hole]), _Part("U1", 10, 10)])
    result = thermal.analyse(board, {"U1": 1.0}, step_mm=1.0)
    assert result["copper_coverage"] < 0.02


def test_a_plated_hole_keeps_its_ring_and_loses_its_hole():
    """The annulus is copper; the drilled middle is air."""
    plated = _Pad(20, 15, w=10.0, h=10.0)
    plated.drill = 6.0
    ring = _Board(footprints=[_Part("H1", 20, 15, pads=[plated]), _Part("U1", 10, 10)])
    solid = _Board(
        footprints=[_Part("H1", 20, 15, pads=[_Pad(20, 15, w=10.0, h=10.0)]), _Part("U1", 10, 10)]
    )
    with_hole = thermal.analyse(ring, {"U1": 1.0}, step_mm=1.0)["copper_coverage"]
    without = thermal.analyse(solid, {"U1": 1.0}, step_mm=1.0)["copper_coverage"]
    assert with_hole < without


def test_a_part_wholly_off_the_board_is_refused_not_smeared_onto_the_edge():
    board = _Board(footprints=[_Part("U1", -30.0, -30.0)])
    with pytest.raises(ValueError, match="outside"):
        thermal.analyse(board, {"U1": 1.0}, step_mm=1.0)


def test_a_rotated_courtyard_heats_its_polygon_not_its_box():
    """A diamond of half the box's area takes the same watts twice as densely."""
    diamond = _Part("U1", 20, 15, half=6.0, courtyard=[(20, 9), (26, 15), (20, 21), (14, 15)])
    boxed = _Part("U2", 20, 15, half=6.0)
    turned = thermal.analyse(_Board(footprints=[diamond]), {"U1": 1.0}, step_mm=1.0)
    square = thermal.analyse(_Board(footprints=[boxed]), {"U2": 1.0}, step_mm=1.0)
    assert turned["max_rise_c"] > square["max_rise_c"] * 1.1


def test_a_declared_thickness_beats_the_default_when_the_stackup_is_silent():
    board = _Board(footprints=[_Part("U1", 10, 10)])
    board.stackup = []
    board.setup = {"thickness": 0.8}
    result = thermal.analyse(board, {"U1": 1.0}, step_mm=1.0)
    assert result["board_thickness_mm"] == pytest.approx(0.8)


def test_the_board_thickness_comes_from_the_real_stackup_vocabulary():
    """KiCad says core and prepreg, not 'dielectric' - both must be summed."""
    board = _Board(footprints=[_Part("U1", 10, 10)])
    board.stackup = [
        {"name": "F.Cu", "type": "copper", "thickness": 0.035},
        {"name": "dielectric 1", "type": "prepreg", "thickness": 0.2},
        {"name": "dielectric 2", "type": "core", "thickness": 0.71},
        {"name": "B.Cu", "type": "copper", "thickness": 0.035},
    ]
    result = thermal.analyse(board, {"U1": 1.0}, step_mm=1.0)
    assert result["board_thickness_mm"] == pytest.approx(0.91)


def test_an_offset_drill_removes_copper_where_the_hole_is():
    """One hole, in its own place: not a second, fictitious, centred one."""
    pad = _Pad(20, 15, w=10.0, h=10.0)
    pad.drill = 2.0
    pad.drill_size = (2.0, 2.0)
    pad.drill_offset = (3.0, 0.0)
    board = _Board(footprints=[_Part("H1", 20, 15, half=5.0, pads=[pad]), _Part("U1", 8, 8)])
    masks, holes = thermal._copper_masks(board, 0.0, 0.0, 80, 60, 0.5)
    front = masks["F.Cu"]

    def cell(x, y):
        return int(y / 0.5), int(x / 0.5)

    # the pad's own copper stays whole; only `holes` takes the drill out
    assert front[cell(20, 15)]  # the pad centre is copper, no hole there
    assert front[cell(23, 15)]  # ...and so is the annulus around the real hole
    assert holes[cell(23, 15)]  # the hole itself is where the file put it
    assert not holes[cell(20, 15)]


def test_an_elongated_pad_lands_on_the_diagonal_kicad_drew_it_on():
    """Board space to pad-local is the inverse of KiCad's rotation.

    Applying the forward rotation instead mirrors the pad: a 4x1 land at
    45 degrees rasterizes along the other diagonal, which invents copper
    beside whatever it should have missed.
    """
    import numpy as np

    from eda_toolkit.kicad import pcb

    pad = _Pad(20, 15, w=4.0, h=1.0, layers=("F.Cu",))
    pad.shape = "rect"
    pad.angle = 45.0
    board = _Board(footprints=[_Part("U1", 20, 15, half=3.0, pads=[pad])])
    masks, _ = thermal._copper_masks(board, 0.0, 0.0, 400, 300, 0.1)
    ys, xs = np.nonzero(masks["F.Cu"])
    assert len(xs) > 0
    cx, cy = (xs + 0.5) * 0.1, (ys + 0.5) * 0.1

    # KiCad puts the far end of the long axis here; the copper must follow
    _, end_y = pcb._rotate(2.0, 0.0, 45.0)
    assert end_y < 0  # up-and-right on a y-down canvas
    correlation = float(np.corrcoef(cx - 20, cy - 15)[0, 1])
    assert correlation < -0.5, f"pad lies on the wrong diagonal ({correlation:+.2f})"


def test_a_trace_thinner_than_a_cell_does_not_vanish():
    """Copper must not disappear on where the grid's phase happens to fall.

    A 0.2 mm trace on the default half-millimetre grid can pass between every
    cell centre; sampled that way it contributes nothing at all, and whether
    it does depends only on the outline's origin.
    """
    board = _Board()
    board.tracks = [pcb_mod.Track((5.0, 10.0), (35.0, 10.0), 0.2, "F.Cu", 1, "SIG")]
    masks, _ = thermal._copper_masks(board, 0.0, 0.0, 80, 60, 0.5)
    on_grid = int(masks["F.Cu"].sum())

    shifted = _Board()
    shifted.tracks = [pcb_mod.Track((5.0, 10.17), (35.0, 10.17), 0.2, "F.Cu", 1, "SIG")]
    masks2, _ = thermal._copper_masks(shifted, 0.0, 0.0, 80, 60, 0.5)
    off_grid = int(masks2["F.Cu"].sum())

    assert on_grid > 50, f"a 30 mm trace left {on_grid} cells of copper"
    assert abs(on_grid - off_grid) <= 2, "the answer moved with the grid's phase"


def test_a_pad_smaller_than_a_cell_does_not_vanish():
    """The same phase trap the traces had: a fine-pitch pin must stay put."""
    counts = []
    for offset in (0.0, 0.25):
        pad = _Pad(10 + offset, 10 + offset, w=0.2, h=0.2, layers=("F.Cu",))
        pad.shape = "rect"
        board = _Board(footprints=[_Part("U1", 10 + offset, 10 + offset, half=1.0, pads=[pad])])
        masks, _ = thermal._copper_masks(board, 0.0, 0.0, 80, 60, 0.5)
        counts.append(int(masks["F.Cu"].sum()))
    assert all(c >= 1 for c in counts), f"the pad vanished at some phase: {counts}"
    assert counts[0] == counts[1], f"the answer moved with the grid's phase: {counts}"


def _pad(shape, w, h, **kw):
    return pcb_mod.Pad(
        number="1",
        type="smd",
        shape=shape,
        x=20.0,
        y=15.0,
        angle=0.0,
        size=(w, h),
        drill=None,
        layers=["F.Cu"],
        **kw,
    )


def _copper_area_mm2(pad, step=0.05):
    import numpy as np

    board = _Board(footprints=[_Part("U1", 20, 15, half=5.0, pads=[pad])])
    masks, _ = thermal._copper_masks(board, 0.0, 0.0, int(40 / step), int(30 / step), step)
    return float(np.count_nonzero(masks["F.Cu"])) * step * step


@pytest.mark.parametrize("delta", [-1.0, 1.0])
def test_a_trapezoid_pad_keeps_the_area_its_taper_conserves(delta):
    """A pure taper moves copper across the pad; it does not create or destroy it.

    Both signs of ``(rect_delta dy dx)`` widen one end by the same amount they
    narrow the other, so a 4 x 2 land is 8 mm2 whichever way it leans. Reading
    the shape as a plain rectangle passes this test by accident, which is why
    the sibling test below checks that the copper actually leans.
    """
    area = _copper_area_mm2(_pad("trapezoid", 4.0, 2.0, rect_delta=(delta, 0.0)))
    assert area == pytest.approx(8.0, abs=0.05)


def test_a_trapezoid_pad_is_wider_at_the_end_its_delta_names():
    """`(rect_delta dy dx)`: a positive dy is the wide end at increasing y."""
    import numpy as np

    pad = _pad("trapezoid", 4.0, 2.0, rect_delta=(1.0, 0.0))
    board = _Board(footprints=[_Part("U1", 20, 15, half=5.0, pads=[pad])])
    step = 0.05
    masks, _ = thermal._copper_masks(board, 0.0, 0.0, int(40 / step), int(30 / step), step)
    rows = np.nonzero(masks["F.Cu"].any(axis=1))[0]
    top, bottom = int(masks["F.Cu"][rows[0]].sum()), int(masks["F.Cu"][rows[-1]].sum())
    assert bottom > top * 1.5, (
        f"the taper did not lean: {top} cells at the top, {bottom} at the bottom"
    )


def test_a_chamfered_pad_loses_a_triangle_at_each_named_corner():
    """cut = ratio * short side; each corner named drops a cut x cut triangle.

    A 4 x 4 land with ratio 0.25 has a 1 mm cut, so two chamfered corners take
    2 x 0.5 mm2 off 16 mm2 and leave 15.
    """
    pad = _pad(
        "chamfered_rect",
        4.0,
        4.0,
        chamfer_ratio=0.25,
        chamfer_corners=["top_left", "bottom_right"],
    )
    assert _copper_area_mm2(pad) == pytest.approx(15.0, abs=0.05)


def test_a_chamfered_pad_with_no_corners_named_is_the_whole_rectangle():
    """`chamfer_ratio` alone cuts nothing: KiCad names the corners separately."""
    pad = _pad("chamfered_rect", 4.0, 4.0, chamfer_ratio=0.25)
    assert _copper_area_mm2(pad) == pytest.approx(16.0, abs=0.05)
