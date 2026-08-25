"""A 2D quasi-static field solve for trace cross-sections.

The closed forms in `electrical.py` answer the geometries they were fitted to.
This module answers the ones they were not: a differential pair at an arbitrary
gap, a stripline checked against something that is not another fit, and any
cross-section a caller can pose as a grid.

The method is the oldest one there is. The cross-section becomes a grid of
cells, each carrying a permittivity; the trace is held at 1 V and the planes at
0; the potential solves div(eps * grad(phi)) = 0 by red-black successive
over-relaxation; and the capacitance per metre falls out of the field energy.
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


def _relax(
    phi: np.ndarray,
    fixed: np.ndarray,
    eps: np.ndarray,
    *,
    closed_top: bool,
    max_sweeps: int,
    tol: float = 1e-7,
) -> np.ndarray:
    """Red-black SOR on div(eps grad phi) = 0, Dirichlet where ``fixed``.

    The open boundaries (sides, and the top of a microstrip box) are Neumann,
    imposed by copying the neighbouring row after each half-sweep - the field
    leaves the box normally instead of being pinned to zero, which would
    squeeze the fringing capacitance.
    """
    p = phi.copy()
    ny, nx = p.shape
    e = eps
    ee = 0.5 * (e[1:-1, 1:-1] + e[1:-1, 2:])
    ew = 0.5 * (e[1:-1, 1:-1] + e[1:-1, :-2])
    en = 0.5 * (e[1:-1, 1:-1] + e[2:, 1:-1])
    es = 0.5 * (e[1:-1, 1:-1] + e[:-2, 1:-1])
    total = ee + ew + en + es
    jj, ii = np.meshgrid(np.arange(1, nx - 1), np.arange(1, ny - 1))
    interior_free = ~fixed[1:-1, 1:-1]
    colours = [((ii + jj) % 2 == parity) & interior_free for parity in (0, 1)]
    # the classic optimal over-relaxation for a Laplace problem of this size
    omega = 2.0 / (1.0 + math.sin(math.pi / max(nx, ny)))
    check_every = 25
    for sweep in range(max_sweeps):
        largest = 0.0
        for colour in colours:
            target = (
                ee * p[1:-1, 2:] + ew * p[1:-1, :-2] + en * p[2:, 1:-1] + es * p[:-2, 1:-1]
            ) / total
            step = omega * (target - p[1:-1, 1:-1])
            if sweep % check_every == 0:
                largest = max(largest, float(np.max(np.abs(np.where(colour, step, 0.0)))))
            p[1:-1, 1:-1] += np.where(colour, step, 0.0)
            p[:, 0] = p[:, 1]
            p[:, -1] = p[:, -2]
            if not closed_top:
                p[-1, :] = p[-2, :]
        if sweep % check_every == 0 and largest < tol:
            break
    return p


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


def _upsample(p: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    """The coarse potential stretched onto the fine grid, as a starting guess."""
    grown = np.repeat(np.repeat(p, 2, axis=0), 2, axis=1)
    out = np.zeros(shape)
    ny, nx = min(shape[0], grown.shape[0]), min(shape[1], grown.shape[1])
    out[:ny, :nx] = grown[:ny, :nx]
    if ny < shape[0]:
        out[ny:, :] = out[ny - 1, :]
    if nx < shape[1]:
        out[:, nx:] = out[:, nx - 1 : nx]
    return out


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

    def grids(self, scale: int, epsilon_r: float, vacuum: bool):
        """Permittivity, boundary potential and fixed mask at ``scale``x."""
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
            phi[trace_bottom:top, x0 : x0 + n_w] = -1.0
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

    def one(scale: int, warm: np.ndarray | None, sweeps: int):
        eps, phi, fixed = counts.grids(scale, epsilon_r, vacuum=False)
        start = phi if warm is None else np.where(fixed, phi, _upsample(warm, phi.shape))
        solved = _relax(start, fixed, eps, closed_top=counts.stripline, max_sweeps=sweeps)
        c_die = energy_to_c * _face_energy(solved, eps)
        ones = np.ones_like(eps)
        solved_air = _relax(solved, fixed, ones, closed_top=counts.stripline, max_sweeps=sweeps)
        return c_die, energy_to_c * _face_energy(solved_air, ones), solved

    c1, a1, warm = one(1, None, 4000)
    c2, a2, _ = one(2, warm, 6000)

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
            "method": "2D quasi-static, red-black SOR, Richardson-extrapolated",
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
