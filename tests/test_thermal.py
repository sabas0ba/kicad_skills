"""The thermal solve, held to conservation and to hand calculations.

The strongest reference is the one the physics gives for free: at steady state
every watt dissipated is a watt convected, whatever the geometry. The second is
the bath: a uniformly heated plate with adiabatic edges has no gradients, and
its rise is P / (h * A * 2) on a calculator. Everything else is comparative,
which is how the tool is meant to be read anyway.
"""

import math

import pytest

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
