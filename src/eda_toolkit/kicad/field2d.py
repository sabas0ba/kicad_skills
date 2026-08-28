"""A 2D quasi-static field solve for trace cross-sections.

The closed forms in `electrical.py` answer the geometries they were fitted to.
This module answers the ones they were not: a differential pair at an arbitrary
gap, a stripline checked against something that is not another fit, and any
cross-section a caller can pose as a grid.

The method is the oldest one there is. The cross-section becomes a grid of
cells, each carrying a permittivity; the trace is held at 1 V and the planes at
0; the potential solves div(eps * grad(phi)) = 0, discretised on the 5-point
stencil and solved exactly by sparse LU - one factorisation answers every
excitation of the same geometry; and the capacitance per metre falls out of
the field energy.
Solving the same geometry with the dielectric replaced by vacuum gives the
air-line capacitance, and

    Z0 = 1 / (c * sqrt(C * C_air)),        eps_eff = C / C_air.

Three decisions make the numbers trustworthy rather than merely plausible, and
each earned its place by being wrong first:

* The energy is measured with the *same* face-difference operator the
  relaxation solves. Measured with a central difference instead, the mismatch
  read as a capacitance a quarter too small.
* The copper is snapped to the coarse grid once, and both grids solve that
  *identical* geometry. Rounding the width independently on each grid solves
  two slightly different problems, and the Richardson extrapolation then
  amplifies their disagreement instead of cancelling the discretisation error.
* What the snap changed is handed back by the closed form: the answer is
  corrected by ``reference(asked) - reference(snapped)``. The reference's
  absolute level never enters - only its local slope - so the correction stays
  honest even where the fit's level is off, and it vanishes as the grid
  refines.

What this is not: a full-wave solver. It is quasi-static, so it knows nothing
of dispersion, loss, or surface roughness, and above a few GHz on thick
laminates those start to matter. It answers the same question the closed forms
answer - what impedance does this geometry make - without their fitted validity
band.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

import numpy as np

from . import electrical

C_LIGHT_M_S = 299_792_458.0
EPSILON_0 = 8.8541878128e-12

# How far the solved box extends past the copper, in substrate heights. The
# fringing field of a microstrip decays over a few heights; twelve is where
# widening the box moves the answer by less than the mesh does. A stripline
# needs far less: the grounded planes short the field out laterally within a
# couple of spacings, and a wide box there is only wasted cells.
BOX_MARGIN_HEIGHTS = 12.0
STRIPLINE_MARGIN_SPACINGS = 2.5

# Fine-grid resolution: cells across the *smallest lateral feature* (the trace
# width, or the substrate height if that is smaller). Keying the cell size to
# the smallest feature rather than to the substrate is what keeps a narrow
# stripline from being solved three cells wide.
CELLS_PER_FEATURE = 8

# The fine grid's ceiling. `_solve` holds several float64 arrays of this size
# at once, so ten million cells is already most of a gigabyte - past that the
# answer arrives slower than a fab's calculator and possibly not at all.
MAX_GRID_CELLS = 10_000_000


def _factorized(eps: np.ndarray, fixed: np.ndarray, *, closed_top: bool):
    """One LU factorisation of div(eps grad phi) = 0 on this grid.

    Returns a function taking the boundary potentials (the ``phi`` array with
    values on the ``fixed`` cells) and handing back the solved field. The
    matrix depends only on the permittivities and on *where* the copper is,
    not on what it is held at - so the odd and even excitations of a pair,
    which is what a capacitance matrix needs, share one factorisation.

    The stencil is the same face-averaged 5-point operator `_face_energy`
    measures, Dirichlet on ``fixed`` cells, and Neumann elsewhere on the box:
    the sides (and the top of a microstrip box) mirror their inner neighbour,
    so the field leaves normally instead of being pinned to zero, which would
    squeeze the fringing capacitance. A direct solve has no sweep budget to
    exhaust: the residual is machine precision at every size the box guard
    admits, where the relaxation this replaced ran out of iterations on tall
    thin cross-sections and quietly handed back an unconverged field.
    """
    from scipy import sparse
    from scipy.sparse.linalg import splu

    ny, nx = eps.shape
    n = ny * nx
    index = np.arange(n).reshape(ny, nx)

    # who owns each cell's row: Dirichlet beats the open-top mirror beats the
    # side mirrors beats the interior stencil - the same precedence the
    # relaxation's copy order produced
    dirichlet = fixed.copy()
    dirichlet[0, :] = True
    if closed_top:
        dirichlet[-1, :] = True
    top_mirror = np.zeros_like(dirichlet)
    if not closed_top:
        top_mirror[-1, :] = ~dirichlet[-1, :]
    side_mirror = np.zeros_like(dirichlet)
    side_mirror[:, 0] = ~(dirichlet[:, 0] | top_mirror[:, 0])
    side_mirror[:, -1] = ~(dirichlet[:, -1] | top_mirror[:, -1])
    interior = ~(dirichlet | top_mirror | side_mirror)

    rows: list[np.ndarray] = []
    cols: list[np.ndarray] = []
    vals: list[np.ndarray] = []

    def add(r: np.ndarray, c: np.ndarray, v: np.ndarray) -> None:
        rows.append(r.ravel())
        cols.append(c.ravel())
        vals.append(np.broadcast_to(v, r.shape).ravel().astype(float))

    ii = index[dirichlet]
    add(ii, ii, np.ones_like(ii, dtype=float))
    ii = index[top_mirror]
    add(ii, ii, np.ones_like(ii, dtype=float))
    add(ii, ii - nx, -np.ones_like(ii, dtype=float))
    for col, inward in ((0, 1), (nx - 1, -1)):
        ii = index[side_mirror[:, col], col]
        add(ii, ii, np.ones_like(ii, dtype=float))
        add(ii, ii + inward, -np.ones_like(ii, dtype=float))

    centre = eps[1:-1, 1:-1]
    faces = {
        (0, 1): 0.5 * (centre + eps[1:-1, 2:]),
        (0, -1): 0.5 * (centre + eps[1:-1, :-2]),
        (1, 0): 0.5 * (centre + eps[2:, 1:-1]),
        (-1, 0): 0.5 * (centre + eps[:-2, 1:-1]),
    }
    free = interior[1:-1, 1:-1]
    ii = index[1:-1, 1:-1][free]
    total = sum(faces.values())[free]
    add(ii, ii, -total)
    for (dy, dx), face in faces.items():
        add(ii, ii + dy * nx + dx, face[free])

    matrix = sparse.csc_matrix(
        (np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))), shape=(n, n)
    )
    # measured on the tallest box the mesh guard admits: minimum-degree on
    # A^T A halves the factorisation against the COLAMD default here
    solve = splu(matrix, permc_spec="MMD_ATA").solve

    def apply(phi: np.ndarray) -> np.ndarray:
        b = np.zeros(n)
        b[index[dirichlet]] = phi[dirichlet]
        return solve(b).reshape(ny, nx)

    return apply


def _face_energy(p: np.ndarray, eps: np.ndarray) -> float:
    """The field energy of the operator the relaxation solved, per volt-metre.

    On a uniform 2D grid the cell size cancels - (dphi/h)^2 * h^2 = dphi^2 -
    so the energy is a plain sum over faces, each weighted by the permittivity
    averaged onto it exactly as the relaxation averaged it.
    """
    ex = 0.5 * (eps[:, :-1] + eps[:, 1:])
    ey = 0.5 * (eps[:-1, :] + eps[1:, :])
    return (
        0.5
        * EPSILON_0
        * (
            float(np.sum(ex * np.diff(p, axis=1) ** 2))
            + float(np.sum(ey * np.diff(p, axis=0) ** 2))
        )
    )


class _Counts:
    """One geometry, snapped once to the coarse grid and held there.

    Everything is an integer number of coarse cells, so scaling by two gives
    the fine grid the *same* copper - which is the whole point: the pair of
    solves must disagree only in cell size, never in what they are solving.
    """

    def __init__(
        self,
        *,
        width_mm: float,
        thickness_mm: float,
        height_mm: float,
        gap_mm: float | None,
        stripline: bool,
        cells: int,
        below_mm: float | None = None,
    ):
        coarse = max(3, cells // 2)
        if below_mm is None:
            feature = min(width_mm, height_mm)
        else:
            # both clearances bound the mesh: the trace can sit close to
            # either plane, and the thinner gap is the feature to resolve
            above_mm = height_mm - thickness_mm - below_mm
            feature = min(width_mm, below_mm, above_mm)
        if gap_mm is not None:
            # a tightly coupled pair's gap is where the odd-mode field lives;
            # a mesh that cannot resolve it solves a different pair
            feature = min(feature, gap_mm)
        self.cell_mm = feature / coarse
        self.height = max(2, round(height_mm / self.cell_mm))
        self.width = max(1, round(width_mm / self.cell_mm))
        self.thickness = max(1, round(thickness_mm / self.cell_mm))
        self.gap = max(1, round(gap_mm / self.cell_mm)) if gap_mm is not None else None
        # an asymmetric stripline states where the trace sits over the bottom
        # plane; None keeps the centred default
        self.below = max(1, round(below_mm / self.cell_mm)) if below_mm is not None else None
        margin_mm = (STRIPLINE_MARGIN_SPACINGS if stripline else BOX_MARGIN_HEIGHTS) * height_mm
        self.margin = max(4, round(margin_mm / self.cell_mm))
        self.stripline = stripline

        # The mesh is uniform, so the smallest feature sets the cell size for
        # the whole box - including the margin the fringing field needs. A gap
        # a hundred times finer than the substrate therefore costs a hundred
        # times the cells in each direction, and the solve would ask for tens
        # of gigabytes rather than the few seconds the guide promises. Refuse
        # with the numbers rather than allocate. There is no knob that rescues
        # a ratio this wide - even one cell per feature leaves the margin
        # enormous - so the message says what would have to change instead of
        # promising a setting that cannot reach.
        fine = self.grid_cells(scale=2)
        if fine > MAX_GRID_CELLS:
            raise ValueError(
                f"this cross-section needs {fine / 1e6:.1f} M cells at {self.cell_mm / 2:.5f} mm "
                f"(the limit is {MAX_GRID_CELLS / 1e6:.0f} M): its smallest feature is "
                f"{feature:.4f} mm and the box around it is {margin_mm:.2f} mm, a ratio a "
                "uniform mesh cannot afford. A locally refined solver answers this one; "
                "this module does not."
            )

    def grid_cells(self, scale: int) -> int:
        """How many cells ``grids(scale)`` will allocate, without allocating."""
        kh = self.height * scale
        n_w = self.width * scale
        n_t = self.thickness * scale
        n_m = self.margin * scale
        n_g = self.gap * scale if self.gap is not None else 0
        traces = 1 if self.gap is None else 2
        nx = 2 * n_m + traces * n_w + n_g
        ny = kh + 1 if self.stripline else kh + n_t + n_m
        return nx * ny

    def snapped(self) -> dict[str, float]:
        h = self.cell_mm
        out = {
            "width_mm": self.width * h,
            "thickness_mm": self.thickness * h,
            "height_mm": self.height * h,
        }
        if self.gap is not None:
            out["gap_mm"] = self.gap * h
        if self.below is not None:
            out["below_mm"] = self.below * h
        return out

    def grids(self, scale: int, epsilon_r: float, vacuum: bool, pair_v: float = -1.0):
        """Permittivity, boundary potential and fixed mask at ``scale``x.

        ``pair_v`` is the second trace's potential when there is one: -1 V is
        the odd mode every differential impedance is about, +1 V the even mode
        a capacitance matrix needs as its second equation.
        """
        kh = self.height * scale
        n_w = self.width * scale
        n_t = self.thickness * scale
        n_m = self.margin * scale
        n_g = self.gap * scale if self.gap is not None else 0
        traces = 1 if self.gap is None else 2
        nx = 2 * n_m + traces * n_w + n_g
        if self.stripline:
            ny = kh + 1
            if self.below is not None:
                trace_bottom = min(max(1, self.below * scale), max(1, kh - n_t - 1))
            else:
                trace_bottom = max(1, (kh - n_t) // 2)
        else:
            ny = kh + n_t + n_m
            trace_bottom = kh
        eps = np.ones((ny, nx))
        if not vacuum:
            if self.stripline:
                eps[:, :] = epsilon_r
            else:
                eps[:kh, :] = epsilon_r
        fixed = np.zeros((ny, nx), dtype=bool)
        phi = np.zeros((ny, nx))
        fixed[0, :] = True
        if self.stripline:
            fixed[-1, :] = True
        top = min(ny - 1, trace_bottom + n_t)
        fixed[trace_bottom:top, n_m : n_m + n_w] = True
        phi[trace_bottom:top, n_m : n_m + n_w] = 1.0
        if self.gap is not None:
            x0 = n_m + n_w + n_g
            fixed[trace_bottom:top, x0 : x0 + n_w] = True
            phi[trace_bottom:top, x0 : x0 + n_w] = pair_v
        return eps, phi, fixed


def _solve(
    counts: _Counts,
    epsilon_r: float,
    reference: Callable[[dict[str, float]], float] | None,
    asked: dict[str, float],
    eps_reference: Callable[[dict[str, float]], float] | None = None,
) -> dict[str, Any]:
    """Coarse seeds fine, Richardson to zero cell size, closed-form snap delta.

    A staircased conductor edge makes the leading error first order in the
    cell size, so with the fine grid at half the coarse spacing the
    extrapolation is z* = 2*z_fine - z_coarse. Both raw values stay in the
    meta, because an extrapolation whose inputs are hidden is a number nobody
    can argue with - and so does the snap correction, for the same reason.
    """

    # For one trace at 1 V the energy is E = C/2, so C = 2E. For the pair at
    # +1 V and -1 V the capacitance-matrix algebra collapses to E = C11 - C12,
    # which *is* the odd-mode capacitance per line - no factor of two. Getting
    # this wrong halves every differential impedance, silently.
    energy_to_c = 1.0 if counts.gap is not None else 2.0

    def one(scale: int):
        eps, phi, fixed = counts.grids(scale, epsilon_r, vacuum=False)
        solved = _factorized(eps, fixed, closed_top=counts.stripline)(phi)
        c_die = energy_to_c * _face_energy(solved, eps)
        ones = np.ones_like(eps)
        solved_air = _factorized(ones, fixed, closed_top=counts.stripline)(phi)
        return c_die, energy_to_c * _face_energy(solved_air, ones)

    c1, a1 = one(1)
    c2, a2 = one(2)

    def z0(c_die: float, c_air: float) -> float:
        return 1.0 / (C_LIGHT_M_S * math.sqrt(c_die * c_air))

    z_coarse, z_fine = z0(c1, a1), z0(c2, a2)
    z_star = 2 * z_fine - z_coarse
    eps_star = 2 * (c2 / a2) - (c1 / a1)
    snapped = counts.snapped()
    delta = 0.0
    if reference is not None:
        delta = reference(asked) - reference(snapped)
    # the grids solved the snapped copper, so eps_eff needs the same slope
    # correction the impedance gets - without it the number describes the
    # snapped trace, which the default mesh can thicken appreciably
    eps_delta = 0.0
    if eps_reference is not None:
        eps_delta = eps_reference(asked) - eps_reference(snapped)
    return {
        "z0_ohm": round(z_star + delta, 2),
        "eps_eff": round(eps_star + eps_delta, 3),
        "meta": {
            "method": "2D quasi-static, sparse direct solve, Richardson-extrapolated",
            "z0_coarse_ohm": round(z_coarse, 2),
            "z0_fine_ohm": round(z_fine, 2),
            "snapped": {k: round(v, 4) for k, v in snapped.items()},
            "snap_correction_ohm": round(delta, 2),
            "cell_mm": round(counts.cell_mm / 2, 5),
        },
    }


def microstrip(
    width_mm: float,
    thickness_mm: float,
    height_mm: float,
    epsilon_r: float,
    *,
    cells: int = CELLS_PER_FEATURE,
) -> dict[str, Any]:
    """Single-ended microstrip: the trace on the outer layer, one plane below."""
    if min(width_mm, thickness_mm, height_mm) <= 0 or epsilon_r < 1:
        raise ValueError("geometry must be positive and epsilon_r at least 1")
    counts = _Counts(
        width_mm=width_mm,
        thickness_mm=thickness_mm,
        height_mm=height_mm,
        gap_mm=None,
        stripline=False,
        cells=cells,
    )
    asked = {"width_mm": width_mm, "thickness_mm": thickness_mm, "height_mm": height_mm}

    def reference(g: dict[str, float]) -> float:
        return electrical.hammerstad_jensen_microstrip(
            g["width_mm"], g["thickness_mm"], g["height_mm"], epsilon_r
        )[0]

    def eps_reference(g: dict[str, float]) -> float:
        return electrical.hammerstad_jensen_microstrip(
            g["width_mm"], g["thickness_mm"], g["height_mm"], epsilon_r
        )[1]

    return _solve(counts, epsilon_r, reference, asked, eps_reference)


def differential_microstrip(
    width_mm: float,
    thickness_mm: float,
    height_mm: float,
    epsilon_r: float,
    gap_mm: float,
    *,
    cells: int = CELLS_PER_FEATURE,
) -> dict[str, Any]:
    """A coupled pair driven odd - the mode a differential impedance is about.

    The two traces sit at +1 V and -1 V, the symmetry plane between them is a
    virtual ground, and the energy of that field *is* the odd-mode capacitance
    per line. Zdiff is twice the odd-mode impedance. Unlike the exponential
    fit in `electrical.differential_impedance`, the gap here is real geometry,
    not a correction factor - which is the reason to reach for this function.
    """
    if min(width_mm, thickness_mm, height_mm, gap_mm) <= 0 or epsilon_r < 1:
        raise ValueError("geometry must be positive and epsilon_r at least 1")
    counts = _Counts(
        width_mm=width_mm,
        thickness_mm=thickness_mm,
        height_mm=height_mm,
        gap_mm=gap_mm,
        stripline=False,
        cells=cells,
    )
    asked = {
        "width_mm": width_mm,
        "thickness_mm": thickness_mm,
        "height_mm": height_mm,
        "gap_mm": gap_mm,
    }

    def reference(g: dict[str, float]) -> float:
        single = electrical.hammerstad_jensen_microstrip(
            g["width_mm"], g["thickness_mm"], g["height_mm"], epsilon_r
        )[0]
        return (
            electrical.differential_impedance(
                single, g["gap_mm"], g["height_mm"], kind="microstrip"
            )
            / 2.0
        )

    result = _solve(counts, epsilon_r, reference, asked)
    result["z_odd_ohm"] = result.pop("z0_ohm")
    result["zdiff_ohm"] = round(2 * result["z_odd_ohm"], 2)
    return result


def stripline(
    width_mm: float,
    thickness_mm: float,
    plane_spacing_mm: float,
    epsilon_r: float,
    *,
    trace_below_mm: float | None = None,
    cells: int = CELLS_PER_FEATURE,
) -> dict[str, Any]:
    """A trace between two planes, laminate throughout.

    Centred by default; ``trace_below_mm`` states the dielectric under the
    trace when the stackup is asymmetric - a real cross-section the IPC fit
    cannot pose at all, and a second thing only the solver answers.

    Here the solve doubles as a referee: the IPC-2141 stripline fit had no
    second model to be checked against, and now it has one. eps_eff is not
    reported - in a homogeneous dielectric it is epsilon_r by definition, and
    the solve confirming that is a test, not an output.
    """
    if min(width_mm, thickness_mm, plane_spacing_mm) <= 0 or epsilon_r < 1:
        raise ValueError("geometry must be positive and epsilon_r at least 1")
    if thickness_mm >= plane_spacing_mm:
        raise ValueError("the trace is thicker than the gap between the planes")
    if trace_below_mm is not None and not (0 < trace_below_mm < plane_spacing_mm - thickness_mm):
        raise ValueError("the trace must sit between the planes")
    counts = _Counts(
        width_mm=width_mm,
        thickness_mm=thickness_mm,
        height_mm=plane_spacing_mm,
        gap_mm=None,
        stripline=True,
        cells=cells,
        below_mm=trace_below_mm,
    )
    asked = {
        "width_mm": width_mm,
        "thickness_mm": thickness_mm,
        "height_mm": plane_spacing_mm,
    }

    def reference(g: dict[str, float]) -> float:
        return electrical.stripline_impedance(
            g["width_mm"], g["thickness_mm"], g["height_mm"], epsilon_r
        )

    result = _solve(counts, epsilon_r, reference, asked)
    result.pop("eps_eff", None)
    return result


def coupled_matrices(
    width_mm: float,
    thickness_mm: float,
    height_mm: float,
    epsilon_r: float,
    gap_mm: float,
    *,
    stripline: bool = False,
    trace_below_mm: float | None = None,
    cells: int = CELLS_PER_FEATURE,
) -> dict[str, Any]:
    """The capacitance and inductance matrices of a symmetric coupled pair.

    Two solves instead of one: the pair driven odd (+1 V, -1 V) has energy
    C11 + Cm and driven even (+1 V, +1 V) energy C11 - Cm, so the two
    energies *are* the matrix, no extra machinery. The inductance matrix
    comes from the vacuum solve the impedance already needs: with the
    dielectric removed the medium is homogeneous, TEM holds exactly, and
    [L] = mu0*eps0 * [C_air]^-1.

    This is what crosstalk is made of. The ratios Cm/C11 and Lm/L11 are the
    capacitive and inductive coupling per unit length; in a homogeneous
    dielectric (stripline) they are equal - the matrices are proportional -
    and their difference on an outer layer is precisely why microstrip has
    far-end crosstalk and stripline has none. The tests hold the solver to
    that physics rather than to another fit.
    """
    if min(width_mm, thickness_mm, height_mm, gap_mm) <= 0 or epsilon_r < 1:
        raise ValueError("geometry must be positive and epsilon_r at least 1")
    if stripline and thickness_mm >= height_mm:
        raise ValueError("the trace is thicker than the gap between the planes")
    counts = _Counts(
        width_mm=width_mm,
        thickness_mm=thickness_mm,
        height_mm=height_mm,
        gap_mm=gap_mm,
        stripline=stripline,
        cells=cells,
        below_mm=trace_below_mm,
    )

    def energies(scale: int) -> dict[str, float]:
        eps, phi_odd, fixed = counts.grids(scale, epsilon_r, vacuum=False, pair_v=-1.0)
        _eps, phi_even, _fixed = counts.grids(scale, epsilon_r, vacuum=False, pair_v=1.0)
        out = {}
        for suffix, medium in (("", eps), ("_air", np.ones_like(eps))):
            # the matrix knows where the copper is, not what it is held at,
            # so both excitations ride one factorisation
            solve = _factorized(medium, fixed, closed_top=counts.stripline)
            out["odd" + suffix] = _face_energy(solve(phi_odd), medium)
            out["even" + suffix] = _face_energy(solve(phi_even), medium)
        return out

    coarse = energies(1)
    fine = energies(2)
    # first-order error from the staircased edges, so z* = 2*fine - coarse,
    # applied to each energy before any of them meet in a ratio
    w = {key: 2 * fine[key] - coarse[key] for key in coarse}

    c11 = (w["odd"] + w["even"]) / 2
    cm = (w["odd"] - w["even"]) / 2
    c11_air = (w["odd_air"] + w["even_air"]) / 2
    cm_air = (w["odd_air"] - w["even_air"]) / 2
    if not (0 < cm < c11) or not (0 < cm_air < c11_air):
        raise ValueError(
            "the extrapolated matrices are not passive - the mesh did not "
            "resolve this geometry; widen the gap or refine `cells`"
        )
    det = c11_air**2 - cm_air**2
    mu0_eps0 = 1.0 / C_LIGHT_M_S**2
    l11 = mu0_eps0 * c11_air / det
    lm = mu0_eps0 * cm_air / det

    def z0(c_die: float, c_air: float) -> float:
        return 1.0 / (C_LIGHT_M_S * math.sqrt(c_die * c_air))

    return {
        "c11_pf_m": round(c11 * 1e12, 3),
        "cm_pf_m": round(cm * 1e12, 4),
        "l11_nh_m": round(l11 * 1e9, 2),
        "lm_nh_m": round(lm * 1e9, 3),
        "capacitive_coupling": round(cm / c11, 5),
        "inductive_coupling": round(lm / l11, 5),
        "z_odd_ohm": round(z0(w["odd"], w["odd_air"]), 2),
        "z_even_ohm": round(z0(w["even"], w["even_air"]), 2),
        "delay_ns_m": round(math.sqrt(l11 * c11) * 1e9, 3),
        "meta": {
            "method": "2D quasi-static, odd+even direct solves, Richardson-extrapolated",
            "snapped": {k: round(v, 4) for k, v in counts.snapped().items()},
            "cell_mm": round(counts.cell_mm / 2, 5),
        },
    }
