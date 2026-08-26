"""The field solver, held to references that are not other solvers.

Three anchors, in order of authority: the parallel-plate limit, which involves
no model at all; Hammerstad-Jensen inside the band where it is known good to a
percent or two; and internal consistency - a differential pair pulled apart
must forget it is a pair.
"""

import math

import pytest

from eda_toolkit.kicad import electrical, field2d

H, T, ER = 0.51, 0.035, 4.3


def test_the_solver_agrees_with_hammerstad_jensen_inside_its_band():
    for width in (0.5, 0.85):
        reference, _ = electrical.hammerstad_jensen_microstrip(width, T, H, ER)
        solved = field2d.microstrip(width, T, H, ER)
        assert abs(solved["z0_ohm"] - reference) / reference < 0.04, (
            f"w={width}: solver {solved['z0_ohm']} vs Hammerstad-Jensen {reference}"
        )
        assert 1.0 < solved["eps_eff"] < ER


def test_the_solver_approaches_the_parallel_plate_limit():
    """The one reference with no model in it: a wide trace is a capacitor.

    Z0 -> eta0*h/(W*sqrt(er)) exactly as W/h grows, from below - the fringing
    field the limit ignores is real capacitance. The IPC-2141 fit returns a
    negative number here; the solver has no band to leave.
    """
    width = 20 * H
    plate = electrical.ETA0_OHM * H / (width * math.sqrt(ER))
    solved = field2d.microstrip(width, T, H, ER)["z0_ohm"]
    assert 0 < solved < plate, f"{solved} is not below the plate limit {plate}"
    assert (plate - solved) / plate < 0.15, f"{solved} should be within 15% of {plate}"


def test_a_differential_pair_forgets_it_is_a_pair_as_the_gap_widens():
    single = field2d.microstrip(0.5, T, H, ER)["z0_ohm"]
    previous = 0.0
    for gap in (0.2, 0.5, 4.0):
        zdiff = field2d.differential_microstrip(0.5, T, H, ER, gap)["zdiff_ohm"]
        assert zdiff > previous, f"gap {gap} did not raise Zdiff"
        previous = zdiff
    # at eight substrate heights of separation the lines barely see each other
    assert abs(previous - 2 * single) / (2 * single) < 0.05
    # and tightly coupled, the odd mode is far below two uncoupled lines
    tight = field2d.differential_microstrip(0.5, T, H, ER, 0.2)["zdiff_ohm"]
    assert tight < 2 * single * 0.75


def test_the_stripline_solve_referees_the_ipc_fit():
    """The fit had no second model to be checked against; now it has one."""
    for width in (0.2, 0.4):
        fit = electrical.stripline_impedance(width, T, 1.0, ER)
        solved = field2d.stripline(width, T, 1.0, ER)["z0_ohm"]
        assert abs(solved - fit) / fit < 0.08, f"w={width}: solver {solved} vs fit {fit}"


def test_an_off_centre_stripline_sees_the_nearer_plane():
    """More capacitance to the close plane: the impedance must fall."""
    centred = field2d.stripline(0.3, T, 1.2, ER)["z0_ohm"]
    shifted = field2d.stripline(0.3, T, 1.2, ER, trace_below_mm=0.2)["z0_ohm"]
    assert shifted < centred
    with pytest.raises(ValueError):
        field2d.stripline(0.3, T, 1.2, ER, trace_below_mm=1.3)
    # the mesh resolves whichever clearance is thinner: shifting the trace
    # the same distance off centre in either direction is the same problem
    high = field2d.stripline(0.3, T, 1.2, ER, trace_below_mm=1.2 - T - 0.2)["z0_ohm"]
    assert abs(high - shifted) / shifted < 0.03


def test_the_answer_says_what_it_actually_solved():
    """The snap to the grid and its correction are reported, not hidden."""
    result = field2d.microstrip(0.85, T, H, ER)
    meta = result["meta"]
    snapped = meta["snapped"]
    cell = meta["cell_mm"] * 2  # the snap happened on the coarse grid
    assert abs(snapped["width_mm"] - 0.85) <= cell / 2 + 1e-9
    assert {"z0_coarse_ohm", "z0_fine_ohm", "snap_correction_ohm"} <= set(meta)
    # the extrapolation input is visible: fine sits between coarse and the answer
    assert meta["z0_fine_ohm"] < meta["z0_coarse_ohm"]


def test_impossible_geometry_is_refused():
    with pytest.raises(ValueError):
        field2d.microstrip(0.0, T, H, ER)
    with pytest.raises(ValueError):
        field2d.differential_microstrip(0.5, T, H, ER, gap_mm=0.0)
    with pytest.raises(ValueError):
        field2d.differential_microstrip(0.0, T, H, ER, gap_mm=0.5)
    with pytest.raises(ValueError):
        field2d.differential_microstrip(0.5, T, H, epsilon_r=0.5, gap_mm=0.5)
    with pytest.raises(ValueError):
        field2d.stripline(0.5, T, 1.0, epsilon_r=0.5)
    with pytest.raises(ValueError):
        field2d.stripline(0.5, thickness_mm=1.0, plane_spacing_mm=0.8, epsilon_r=ER)


def test_an_unaffordable_mesh_is_refused_with_its_numbers():
    """A gap a hundredth of the substrate would ask for tens of gigabytes.

    The mesh is uniform, so the finest feature sets the cell size for the
    whole box including its margin. Refusing beats allocating - and beats
    quietly solving a gap the mesh cannot see.
    """
    with pytest.raises(ValueError, match="M cells"):
        field2d.differential_microstrip(1.0, 0.035, 1.0, 4.0, gap_mm=0.01)


def test_the_gaps_a_real_pair_uses_are_still_affordable():
    """The guard must not price out the geometry the tool exists to answer.

    The mesh, not the solve: what is under test is that these gaps build a
    grid at all, and the solved answers already have their own tests.
    """
    for gap in (0.1, 0.15, 0.2):
        counts = field2d._Counts(
            width_mm=0.5,
            thickness_mm=0.035,
            height_mm=0.51,
            gap_mm=gap,
            stripline=False,
            cells=field2d.CELLS_PER_FEATURE,
        )
        assert counts.grid_cells(scale=2) < field2d.MAX_GRID_CELLS
