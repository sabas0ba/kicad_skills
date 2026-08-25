"""Copper physics: checked against hand-computable cases and the IPC curve fits."""

import math

import pytest

from eda_toolkit.kicad import electrical


def test_resistance_of_a_known_piece_of_copper():
    """1 oz copper, 1 mm wide, 100 mm long: rho*L/A with rho = 1.68e-8."""
    ohms = electrical.track_resistance(100.0, 1.0, 0.035)
    expected = 1.68e-8 * 0.1 / (1e-3 * 35e-6)
    assert ohms == pytest.approx(expected)
    assert ohms == pytest.approx(0.048, abs=0.001)  # 48 milliohms


def test_resistance_scales_the_way_geometry_says():
    base = electrical.track_resistance(100.0, 1.0, 0.035)
    assert electrical.track_resistance(200.0, 1.0, 0.035) == pytest.approx(2 * base)
    assert electrical.track_resistance(100.0, 2.0, 0.035) == pytest.approx(base / 2)
    assert electrical.track_resistance(100.0, 1.0, 0.070) == pytest.approx(base / 2)


def test_copper_gets_more_resistive_when_hot():
    cold = electrical.track_resistance(100.0, 1.0, 0.035, temperature_c=20)
    hot = electrical.track_resistance(100.0, 1.0, 0.035, temperature_c=70)
    assert hot / cold == pytest.approx(1 + 0.00393 * 50, abs=0.001)


def test_current_capacity_matches_the_ipc_2221_reference_point():
    """The worked example everyone quotes: 1 oz, 10 C rise, external."""
    # 0.5 mm of 1 oz copper is ~27.6 mils^2; the chart gives a bit over 1.5 A.
    amps = electrical.current_capacity(0.5, 0.035, temperature_rise_c=10, external=True)
    assert 1.3 < amps < 1.9
    # An internal layer of the same section carries half, by definition of k.
    internal = electrical.current_capacity(0.5, 0.035, temperature_rise_c=10, external=False)
    assert internal == pytest.approx(amps / 2)


def test_current_capacity_and_temperature_rise_are_inverses():
    amps = electrical.current_capacity(0.4, 0.035, temperature_rise_c=15)
    assert electrical.temperature_rise(amps, 0.4, 0.035) == pytest.approx(15, rel=1e-6)


def test_width_for_current_round_trips():
    width = electrical.width_for_current(2.0, 0.035, temperature_rise_c=10)
    assert electrical.current_capacity(width, 0.035, temperature_rise_c=10) == pytest.approx(
        2.0, rel=1e-6
    )


def test_a_wider_track_runs_cooler():
    narrow = electrical.temperature_rise(2.0, 0.25, 0.035)
    wide = electrical.temperature_rise(2.0, 1.0, 0.035)
    assert wide < narrow


def test_microstrip_lands_near_fifty_ohms_on_a_typical_stackup():
    """0.2 mm trace, 1 oz copper, 0.1 mm prepreg, FR4: the usual 50 ohm recipe."""
    z0 = electrical.microstrip_impedance(0.2, 0.035, 0.1, 4.3)
    assert 40 < z0 < 60


def test_impedance_falls_as_the_trace_widens():
    narrow = electrical.microstrip_impedance(0.15, 0.035, 0.2, 4.3)
    wide = electrical.microstrip_impedance(0.60, 0.035, 0.2, 4.3)
    assert wide < narrow


def test_a_higher_dielectric_constant_lowers_impedance():
    assert electrical.microstrip_impedance(0.2, 0.035, 0.2, 4.5) < electrical.microstrip_impedance(
        0.2, 0.035, 0.2, 3.0
    )


def test_stripline_is_lower_impedance_than_microstrip_for_the_same_gap():
    """Two reference planes instead of one: more capacitance, less impedance."""
    micro = electrical.microstrip_impedance(0.2, 0.035, 0.2, 4.3)
    strip = electrical.stripline_impedance(0.2, 0.035, 0.2, 4.3)
    assert strip < micro


def test_a_tightly_coupled_pair_is_less_than_twice_the_single_ended_value():
    single = electrical.microstrip_impedance(0.2, 0.035, 0.1, 4.3)
    tight = electrical.differential_impedance(single, 0.1, 0.1, kind="microstrip")
    loose = electrical.differential_impedance(single, 5.0, 0.1, kind="microstrip")
    assert tight < loose <= 2 * single
    assert loose == pytest.approx(2 * single, rel=0.01)  # far apart: no coupling left


@pytest.mark.parametrize("target", [50.0, 75.0, 90.0])
def test_solving_for_a_width_reproduces_the_target(target):
    width = electrical.width_for_impedance(target, 0.035, 0.2, 4.3, kind="microstrip")
    assert width is not None
    # measured with the model that answered, which is Hammerstad-Jensen
    assert electrical.hammerstad_jensen_microstrip(width, 0.035, 0.2, 4.3)[0] == pytest.approx(
        target, rel=1e-3
    )


def test_solving_for_a_differential_width_reproduces_the_target():
    width = electrical.width_for_differential_impedance(90.0, 0.035, 0.2, 4.3, kind="microstrip")
    assert width is not None
    single = electrical.hammerstad_jensen_microstrip(width, 0.035, 0.2, 4.3)[0]
    assert electrical.differential_impedance(
        single, width, 0.2, kind="microstrip"
    ) == pytest.approx(90.0, rel=1e-3)


def test_an_unreachable_target_is_reported_rather_than_guessed():
    """A 0.02 mm dielectric cannot give 100 ohms at any sane width."""
    assert electrical.width_for_impedance(100.0, 0.035, 0.02, 4.3, kind="microstrip") is None


def test_geometry_must_be_physical():
    with pytest.raises(ValueError):
        electrical.track_resistance(10.0, 0.0, 0.035)
    with pytest.raises(ValueError):
        electrical.current_capacity(0.2, 0.035, temperature_rise_c=0)
    with pytest.raises(ValueError):
        electrical.microstrip_impedance(0.2, 0.035, 0.0, 4.3)


# -- reading a board -------------------------------------------------------


class _Board:
    def __init__(self, stackup, tracks=(), copper_layers=("F.Cu", "B.Cu")):
        self.stackup = list(stackup)
        self.tracks = list(tracks)
        self.copper_layers = list(copper_layers)


class _Track:
    def __init__(self, net, start, end, width, layer):
        self.net, self.start, self.end, self.width, self.layer = net, start, end, width, layer


TWO_LAYER = [
    {"name": "F.Cu", "type": "copper", "thickness": 0.035, "epsilon_r": None},
    {"name": "dielectric 1", "type": "core", "thickness": 1.51, "epsilon_r": 4.5},
    {"name": "B.Cu", "type": "copper", "thickness": 0.035, "epsilon_r": None},
]


def test_copper_thickness_comes_from_the_stackup_when_there_is_one():
    board = _Board(TWO_LAYER)
    assert electrical.copper_thickness(board, "F.Cu") == (0.035, "stackup")


def test_a_board_without_a_stackup_says_what_it_assumed():
    board = _Board([])
    thickness, source = electrical.copper_thickness(board, "F.Cu")
    assert thickness == electrical.DEFAULT_COPPER_THICKNESS_MM
    assert "assumed" in source


def test_outer_layers_are_microstrip_and_inner_layers_are_stripline():
    four_layer = [
        {"name": "F.Cu", "type": "copper", "thickness": 0.035},
        {"name": "d1", "type": "prepreg", "thickness": 0.1, "epsilon_r": 4.5},
        {"name": "In1.Cu", "type": "copper", "thickness": 0.035},
        {"name": "d2", "type": "core", "thickness": 1.2, "epsilon_r": 4.5},
        {"name": "In2.Cu", "type": "copper", "thickness": 0.035},
        {"name": "d3", "type": "prepreg", "thickness": 0.1, "epsilon_r": 4.5},
        {"name": "B.Cu", "type": "copper", "thickness": 0.035},
    ]
    board = _Board(four_layer, copper_layers=["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
    assert electrical.layer_geometry(board, "F.Cu")["kind"] == "microstrip"
    assert electrical.layer_geometry(board, "F.Cu")["height_mm"] == pytest.approx(0.1)
    inner = electrical.layer_geometry(board, "In1.Cu")
    assert inner["kind"] == "stripline"
    # plane to plane, not to the nearest one
    assert inner["height_mm"] == pytest.approx(1.3)


def test_a_board_with_no_dielectric_data_gets_no_impedance_answer():
    board = _Board([{"name": "F.Cu", "type": "copper", "thickness": 0.035}])
    assert electrical.layer_geometry(board, "F.Cu") is None


def test_the_narrowest_segment_sets_a_nets_current_rating():
    board = _Board(
        TWO_LAYER,
        tracks=[
            _Track("+5V", (0, 0), (10, 0), 1.0, "F.Cu"),
            _Track("+5V", (10, 0), (20, 0), 0.25, "F.Cu"),  # the bottleneck
        ],
    )
    net = electrical.analyse(board)["nets"][0]
    assert net["net"] == "+5V"
    assert net["narrowest_mm"] == 0.25
    assert net["length_mm"] == pytest.approx(20.0)
    assert net["current_a"] == pytest.approx(
        electrical.current_capacity(0.25, 0.035, temperature_rise_c=10), abs=0.01
    )
    # both segments in series, which is the upper bound the note describes
    expected = (
        electrical.track_resistance(10, 1.0, 0.035) + electrical.track_resistance(10, 0.25, 0.035)
    ) * 1000
    assert net["resistance_mohm"] == pytest.approx(expected, rel=1e-6)


def test_nets_are_listed_most_current_limited_first():
    board = _Board(
        TWO_LAYER,
        tracks=[
            _Track("FAT", (0, 0), (10, 0), 2.0, "F.Cu"),
            _Track("THIN", (0, 5), (10, 5), 0.15, "F.Cu"),
        ],
    )
    assert [n["net"] for n in electrical.analyse(board)["nets"]] == ["THIN", "FAT"]


def test_an_internal_layer_is_derated():
    stack = [
        {"name": "F.Cu", "type": "copper", "thickness": 0.035},
        {"name": "d1", "type": "prepreg", "thickness": 0.1, "epsilon_r": 4.5},
        {"name": "In1.Cu", "type": "copper", "thickness": 0.035},
        {"name": "d2", "type": "core", "thickness": 1.2, "epsilon_r": 4.5},
        {"name": "B.Cu", "type": "copper", "thickness": 0.035},
    ]
    board = _Board(
        stack,
        tracks=[
            _Track("OUT", (0, 0), (10, 0), 0.5, "F.Cu"),
            _Track("IN", (0, 5), (10, 5), 0.5, "In1.Cu"),
        ],
        copper_layers=["F.Cu", "In1.Cu", "B.Cu"],
    )
    by_net = {n["net"]: n for n in electrical.analyse(board)["nets"]}
    # the reported values are rounded to milliamps, hence the tolerance
    assert by_net["IN"]["current_a"] == pytest.approx(by_net["OUT"]["current_a"] / 2, abs=0.001)
    assert by_net["IN"]["narrowest_is_external"] is False


def test_the_impedance_table_answers_what_width_this_stackup_needs():
    board = _Board(TWO_LAYER)
    rows = {row["layer"]: row for row in electrical.analyse(board)["impedance"]}
    front = rows["F.Cu"]
    assert front["kind"] == "microstrip"
    # a 1.51 mm core is a long way from a plane, so 50 ohms needs a wide trace
    assert front["width_50r_mm"] > front["width_75r_mm"]
    assert electrical.hammerstad_jensen_microstrip(
        front["width_50r_mm"], 0.035, front["height_mm"], front["epsilon_r"]
    )[0] == pytest.approx(50.0, rel=1e-3)


def test_zero_length_and_unnamed_tracks_are_skipped():
    board = _Board(
        TWO_LAYER,
        tracks=[
            _Track("", (0, 0), (10, 0), 0.5, "F.Cu"),  # no net
            _Track("A", (1, 1), (1, 1), 0.5, "F.Cu"),  # zero length
        ],
    )
    assert electrical.analyse(board)["nets"] == []


def test_the_output_says_what_it_assumed():
    notes = " ".join(electrical.analyse(_Board(TWO_LAYER))["notes"])
    assert "IPC-2221" in notes and "IPC-2141" in notes
    assert "upper bound" in notes


def test_analysis_uses_the_requested_temperature_rise():
    board = _Board(TWO_LAYER, tracks=[_Track("A", (0, 0), (10, 0), 0.5, "F.Cu")])
    cool = electrical.analyse(board, temperature_rise_c=10)["nets"][0]["current_a"]
    hot = electrical.analyse(board, temperature_rise_c=30)["nets"][0]["current_a"]
    assert hot > cool
    assert hot / cool == pytest.approx((30 / 10) ** 0.44, rel=0.01)


def test_math_import_is_used():  # keeps the module honest about its imports
    assert math.isclose(electrical.MM_PER_MIL, 0.0254)


# -- the impedance models, against something that is not another model -------
#
# Both closed forms are fits. Checking one against the other only says they
# agree, so the reference here is the one case that needs no model: as the trace
# grows wide the cross-section becomes a parallel-plate capacitor, and its
# impedance is eta0*h/(W*sqrt(er)) exactly. A model that is right approaches it
# from below - the fringing field it drops is real copper-to-plane capacitance -
# and a model that is wrong wanders off, or changes sign.


def _plate_limit(width_mm: float, height_mm: float, epsilon_r: float) -> float:
    return electrical.ETA0_OHM * height_mm / (width_mm * math.sqrt(epsilon_r))


def test_hammerstad_jensen_approaches_the_parallel_plate_limit():
    height, thickness, epsilon = 0.51, 0.035, 4.3
    errors = []
    for ratio in (5, 10, 20, 50):
        width = ratio * height
        z0, _ = electrical.hammerstad_jensen_microstrip(width, thickness, height, epsilon)
        plate = _plate_limit(width, height, epsilon)
        assert 0 < z0 < plate, f"W/h={ratio}: {z0} is not a physical impedance below the plate"
        errors.append((plate - z0) / plate)
    assert errors == sorted(errors, reverse=True), "the gap to the plate limit must close"
    assert errors[-1] < 0.10, f"W/h=50 should be within 10% of the plate limit, is {errors[-1]:.0%}"


def test_effective_permittivity_stays_between_air_and_the_laminate():
    for ratio in (0.1, 0.5, 1, 2, 5, 20, 50):
        _, eps_eff = electrical.hammerstad_jensen_microstrip(ratio * 0.51, 0.035, 0.51, 4.3)
        assert 1.0 < eps_eff < 4.3, f"W/h={ratio}: eps_eff {eps_eff} is outside air..laminate"
    wide = electrical.hammerstad_jensen_microstrip(50 * 0.51, 0.035, 0.51, 4.3)[1]
    narrow = electrical.hammerstad_jensen_microstrip(0.1 * 0.51, 0.035, 0.51, 4.3)[1]
    assert wide > narrow, "a wide trace keeps more of its field in the laminate"


def test_the_accurate_model_falls_monotonically_as_the_trace_widens():
    """What `_solve_width` bisects on has to be monotonic, or the answer is luck."""
    previous = None
    for width in (0.05, 0.1, 0.3, 0.85, 2.0, 5.0, 10.0):
        z0, _ = electrical.hammerstad_jensen_microstrip(width, 0.035, 0.51, 4.3)
        assert previous is None or z0 < previous, f"width {width} did not lower the impedance"
        previous = z0


def test_the_ipc_fit_is_only_trusted_inside_its_band():
    """Documenting the reason the accurate model is the one that answers.

    The IPC-2141 microstrip fit is a logarithm whose argument passes through 1
    at about W/h = 8; past that it returns a negative impedance. It is kept for
    checking a design that states IPC-2141 as its model, and it is not what
    `width_for_impedance` bisects on.
    """
    assert electrical.microstrip_is_in_band(0.85, 0.51, 4.3)
    assert not electrical.microstrip_is_in_band(5.1, 0.51, 4.3)
    assert electrical.microstrip_impedance(5.1, 0.035, 0.51, 4.3) < 0
    inside, _ = electrical.hammerstad_jensen_microstrip(0.85, 0.035, 0.51, 4.3)
    assert abs(inside - electrical.microstrip_impedance(0.85, 0.035, 0.51, 4.3)) / inside < 0.05


def test_width_for_impedance_lands_on_the_target():
    for target in (50.0, 75.0, 90.0):
        width = electrical.width_for_impedance(target, 0.035, 0.51, 4.3, kind="microstrip")
        assert width, f"no width reaches {target} ohm on this stackup"
        z0, _ = electrical.hammerstad_jensen_microstrip(width, 0.035, 0.51, 4.3)
        assert abs(z0 - target) < 0.5, f"{width} mm gives {z0} ohm, not {target}"


def test_solve_re_measures_the_proposed_widths():
    """The closed form proposes a width; the field solver checks the proposal.

    This is the loop closing: two independent methods, one geometry, and the
    answer has to come back at the target from both. It also pins the output
    contract - the solved figures appear only when asked for, because they cost
    seconds where the fits cost microseconds.
    """
    board = _Board(TWO_LAYER)
    plain = electrical.analyse(board)["impedance"][0]
    assert "width_50r_solved_ohm" not in plain

    solved = electrical.analyse(board, solve=True)["impedance"][0]
    assert abs(solved["width_50r_solved_ohm"] - 50.0) < 2.5
    assert abs(solved["width_100r_diff_solved_ohm"] - 100.0) < 5.0
    assert 1.0 < solved["solved_eps_eff"] < solved["epsilon_r"]


def _four_layer(upper_mm: float, lower_mm: float) -> list[dict]:
    return [
        {"name": "F.Cu", "type": "copper", "thickness": 0.035, "epsilon_r": None},
        {"name": "dielectric 1", "type": "prepreg", "thickness": upper_mm, "epsilon_r": 4.5},
        {"name": "In1.Cu", "type": "copper", "thickness": 0.0175, "epsilon_r": None},
        {"name": "dielectric 2", "type": "core", "thickness": lower_mm, "epsilon_r": 4.5},
        {"name": "In2.Cu", "type": "copper", "thickness": 0.0175, "epsilon_r": None},
        {"name": "dielectric 3", "type": "prepreg", "thickness": upper_mm, "epsilon_r": 4.5},
        {"name": "B.Cu", "type": "copper", "thickness": 0.035, "epsilon_r": None},
    ]


def test_solve_reaches_the_inner_layers_too():
    """--solve must not silently skip the stripline rows of a four-layer board."""
    board = _Board(_four_layer(0.6, 0.6), copper_layers=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"))
    rows = {row["layer"]: row for row in electrical.analyse(board, solve=True)["impedance"]}
    inner = rows["In1.Cu"]
    assert inner["kind"] == "stripline"
    # symmetric gap: the solver referees the fit and lands near the target
    assert abs(inner["width_50r_solved_ohm"] - 50.0) < 5.0


def test_an_asymmetric_stripline_is_solved_where_the_trace_actually_sits():
    """A thin prepreg to one plane is the usual case, and only the solver sees it."""
    board = _Board(_four_layer(0.2, 1.0), copper_layers=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"))
    rows = {row["layer"]: row for row in electrical.analyse(board, solve=True)["impedance"]}
    inner = rows["In1.Cu"]
    assert inner["height_below_mm"] == pytest.approx(1.0)
    # the near plane dominates: the real cross-section sits below the value
    # the symmetric fit proposed the width for
    assert inner["width_50r_solved_ohm"] < 50.0


def test_epsilon_is_weighted_by_thickness_and_a_mixed_gap_is_not_solved():
    """A thin bondply beside a thick core moves the wave by its share of the gap."""
    mixed = _four_layer(0.2, 1.0)
    mixed[1]["epsilon_r"] = 3.0  # dielectric 1, the thin prepreg
    mixed[3]["epsilon_r"] = 10.0  # dielectric 2, the thick core
    board = _Board(mixed, copper_layers=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"))
    rows = {row["layer"]: row for row in electrical.analyse(board, solve=True)["impedance"]}
    inner = rows["In1.Cu"]
    # (0.2*3 + 1.0*10) / 1.2, nothing like the unweighted 6.5
    assert inner["epsilon_r"] == pytest.approx(8.833, abs=0.01)
    assert inner["epsilon_r_range"] == [3.0, 10.0]
    # the solver refuses to pretend the gap is homogeneous
    assert "width_50r_solved_ohm" not in inner
    assert "solve_skipped" in inner
