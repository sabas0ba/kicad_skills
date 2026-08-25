"""Physical electrical properties of the copper, from the board's own stackup.

The review rules answer "is this track suspiciously thin". These answer the
question underneath it - how much current will it actually carry, what does it
cost in resistance, and what width does this stackup need for a controlled
impedance - by doing the arithmetic instead of guessing at a threshold.

Everything here is a closed-form approximation from the IPC standards, computed
from the geometry KiCad already stores. That makes them useful for sizing and
for catching mistakes; they are not a field solver, and the impedance numbers in
particular are worth about +-10%. Where a stackup does not say something, the
fallback is stated in the output rather than hidden.
"""

from __future__ import annotations

import math
from typing import Any

# Annealed copper at 20 C.
COPPER_RESISTIVITY = 1.68e-8  # ohm metre
COPPER_TEMPERATURE_COEFFICIENT = 0.00393  # per kelvin
# 1 oz/ft^2, the default nearly every 2-layer board is made with.
DEFAULT_COPPER_THICKNESS_MM = 0.035
MM_PER_MIL = 0.0254

# IPC-2221 charts, as the usual curve fit: I = k * dT^0.44 * A^0.725.
IPC2221_K_EXTERNAL = 0.048
IPC2221_K_INTERNAL = 0.024
IPC2221_DT_EXPONENT = 0.44
IPC2221_AREA_EXPONENT = 0.725


def track_resistance(
    length_mm: float, width_mm: float, thickness_mm: float, *, temperature_c: float = 20.0
) -> float:
    """Ohms for a rectangular copper track. R = rho * L / A."""
    if width_mm <= 0 or thickness_mm <= 0:
        raise ValueError("width and thickness must be positive")
    area_m2 = (width_mm * 1e-3) * (thickness_mm * 1e-3)
    resistivity = COPPER_RESISTIVITY * (1 + COPPER_TEMPERATURE_COEFFICIENT * (temperature_c - 20.0))
    return resistivity * (length_mm * 1e-3) / area_m2


def current_capacity(
    width_mm: float, thickness_mm: float, *, temperature_rise_c: float = 10.0, external: bool = True
) -> float:
    """Amps this cross-section carries for a given steady-state temperature rise.

    IPC-2221 for a bare track in still air. External layers shed heat and carry
    roughly twice what an internal layer does, which is the whole of the
    difference between the two constants.
    """
    if width_mm <= 0 or thickness_mm <= 0 or temperature_rise_c <= 0:
        raise ValueError("width, thickness and temperature rise must be positive")
    area_mils2 = (width_mm / MM_PER_MIL) * (thickness_mm / MM_PER_MIL)
    k = IPC2221_K_EXTERNAL if external else IPC2221_K_INTERNAL
    return k * temperature_rise_c**IPC2221_DT_EXPONENT * area_mils2**IPC2221_AREA_EXPONENT


def temperature_rise(
    current_a: float, width_mm: float, thickness_mm: float, *, external: bool = True
) -> float:
    """The inverse: kelvin of rise for a current. IPC-2221 rearranged."""
    if current_a <= 0:
        return 0.0
    area_mils2 = (width_mm / MM_PER_MIL) * (thickness_mm / MM_PER_MIL)
    k = IPC2221_K_EXTERNAL if external else IPC2221_K_INTERNAL
    return (current_a / (k * area_mils2**IPC2221_AREA_EXPONENT)) ** (1 / IPC2221_DT_EXPONENT)


def width_for_current(
    current_a: float,
    thickness_mm: float,
    *,
    temperature_rise_c: float = 10.0,
    external: bool = True,
) -> float:
    """Millimetres of width needed to carry a current. IPC-2221 solved for width."""
    if current_a <= 0:
        return 0.0
    k = IPC2221_K_EXTERNAL if external else IPC2221_K_INTERNAL
    area_mils2 = (current_a / (k * temperature_rise_c**IPC2221_DT_EXPONENT)) ** (
        1 / IPC2221_AREA_EXPONENT
    )
    return area_mils2 * MM_PER_MIL * MM_PER_MIL / thickness_mm


# -- controlled impedance --------------------------------------------------
#
# Two models, and the difference between them is not academic.
#
# IPC-2141 is the pair of logarithmic fits most PCB documentation quotes. They
# are quick, they are quoted for W/h between 0.1 and 3, and outside that band
# they do not merely drift: the microstrip fit's logarithm passes through 1 and
# the formula returns a *negative* impedance. A 5 mm power trace on a 0.51 mm
# dielectric - W/h of 10, an ordinary thing to ask about - comes back as -10.9
# ohms.
#
# Hammerstad-Jensen is the accurate one, and it is what this module uses to
# answer questions. It carries a thickness correction, it stays physical out to
# W/h of 50 and beyond, and its effective permittivity climbs toward the bulk
# value the way the physics says it must. `tests/test_electrical.py` holds both
# to the one answer that needs no model at all: as the trace grows wide the
# structure becomes a parallel-plate capacitor, whose impedance is
# eta0*h/(W*sqrt(er)) exactly.
#
#      W/h      plate      IPC-2141        Hammerstad-Jensen
#        2      90.84         46.47                   48.13
#       10      18.17        -10.91                   14.83
#       50       3.63        -69.26                    3.44
#
# IPC-2141 stays, because a board file that states IPC-2141 as its model should
# be able to be checked against it, and because the fits are what a lot of house
# rules were written from.

ETA0_OHM = 376.730313668  # the impedance of free space, mu0*c


def microstrip_impedance(width_mm: float, thickness_mm: float, height_mm: float, epsilon_r: float):
    """Single-ended trace on an outer layer over one reference plane."""
    if min(width_mm, thickness_mm, height_mm) <= 0 or epsilon_r <= 0:
        raise ValueError("geometry and epsilon_r must be positive")
    return (87.0 / math.sqrt(epsilon_r + 1.41)) * math.log(
        5.98 * height_mm / (0.8 * width_mm + thickness_mm)
    )


def stripline_impedance(width_mm: float, thickness_mm: float, height_mm: float, epsilon_r: float):
    """Single-ended trace on an inner layer, between two reference planes.

    ``height_mm`` is the plane-to-plane spacing, not the distance to one plane.
    """
    if min(width_mm, thickness_mm, height_mm) <= 0 or epsilon_r <= 0:
        raise ValueError("geometry and epsilon_r must be positive")
    return (60.0 / math.sqrt(epsilon_r)) * math.log(
        4.0 * height_mm / (0.67 * math.pi * (0.8 * width_mm + thickness_mm))
    )


def hammerstad_jensen_microstrip(
    width_mm: float, thickness_mm: float, height_mm: float, epsilon_r: float
) -> tuple[float, float]:
    """Microstrip impedance and effective permittivity, the accurate way.

    Hammerstad and Jensen's synthesis of the quasi-static microstrip problem,
    with their correction for a trace that has thickness. Returns both numbers
    because the second one is what a delay or a wavelength is calculated from,
    and a model that gives an impedance without saying what dielectric the wave
    thinks it is in has answered half the question.
    """
    if min(width_mm, height_mm) <= 0 or thickness_mm < 0 or epsilon_r <= 0:
        raise ValueError("geometry and epsilon_r must be positive")
    u = width_mm / height_mm
    t_h = thickness_mm / height_mm
    if t_h > 0:
        # a trace with thickness behaves as a wider trace with none; the two
        # corrections differ because the dielectric fills only one side
        coth = 1.0 / math.tanh(math.sqrt(6.517 * u))
        wide = (t_h / math.pi) * math.log(1.0 + 4.0 * math.e / (t_h * coth * coth))
        u = u + 0.5 * (1.0 + 1.0 / math.cosh(math.sqrt(epsilon_r - 1.0))) * wide

    a = (
        1.0
        + math.log((u**4 + (u / 52.0) ** 2) / (u**4 + 0.432)) / 49.0
        + math.log(1.0 + (u / 18.1) ** 3) / 18.7
    )
    b = 0.564 * ((epsilon_r - 0.9) / (epsilon_r + 3.0)) ** 0.053
    eps_eff = (epsilon_r + 1.0) / 2.0 + (epsilon_r - 1.0) / 2.0 * (1.0 + 10.0 / u) ** (-a * b)

    f = 6.0 + (2.0 * math.pi - 6.0) * math.exp(-((30.666 / u) ** 0.7528))
    z_air = (ETA0_OHM / (2.0 * math.pi)) * math.log(f / u + math.sqrt(1.0 + (2.0 / u) ** 2))
    return z_air / math.sqrt(eps_eff), eps_eff


def differential_impedance(single_ended: float, gap_mm: float, height_mm: float, *, kind: str):
    """Coupled pair, from the single-ended value and how close the two traces run."""
    if kind == "microstrip":
        return 2.0 * single_ended * (1.0 - 0.48 * math.exp(-0.96 * gap_mm / height_mm))
    return 2.0 * single_ended * (1.0 - 0.347 * math.exp(-2.9 * gap_mm / height_mm))


def _model_for(kind: str):
    """The impedance model the toolkit answers with.

    Microstrip goes to Hammerstad-Jensen, which is well behaved everywhere a
    board can be built. Stripline keeps the IPC-2141 fit: it is symmetric, so
    its logarithm has no zero crossing to fall through, and there is no second
    model here to check it against yet.
    """
    if kind == "microstrip":
        return lambda w, t, h, er: hammerstad_jensen_microstrip(w, t, h, er)[0]
    return stripline_impedance


def _solve_width(target_ohm: float, evaluate, *, low: float = 0.02, high: float = 10.0):
    """Bisect for the width that hits a target impedance.

    Impedance falls monotonically as the trace gets wider, which is what makes a
    plain bisection safe here.
    """
    if evaluate(low) < target_ohm or evaluate(high) > target_ohm:
        return None  # the target is not reachable on this stackup
    for _ in range(60):
        middle = (low + high) / 2
        if evaluate(middle) > target_ohm:
            low = middle
        else:
            high = middle
    return (low + high) / 2


def width_for_impedance(
    target_ohm: float, thickness_mm: float, height_mm: float, epsilon_r: float, *, kind: str
) -> float | None:
    """Trace width that gives ``target_ohm`` single-ended on this stackup."""
    model = _model_for(kind)
    return _solve_width(target_ohm, lambda w: model(w, thickness_mm, height_mm, epsilon_r))


def width_for_differential_impedance(
    target_ohm: float, thickness_mm: float, height_mm: float, epsilon_r: float, *, kind: str
) -> float | None:
    """Trace width for a differential target, taking the gap equal to the width.

    Gap and width are two unknowns for one target, so something has to be fixed.
    Equal gap and width is the usual starting point and keeps the answer a single
    number; adjust from there once the router has an opinion.
    """
    model = _model_for(kind)

    def evaluate(width: float) -> float:
        single = model(width, thickness_mm, height_mm, epsilon_r)
        return differential_impedance(single, width, height_mm, kind=kind)

    return _solve_width(target_ohm, evaluate)


def microstrip_is_in_band(width_mm: float, height_mm: float, epsilon_r: float) -> bool:
    """The IPC-2141 microstrip fit is quoted for these ranges."""
    return 0.1 <= width_mm / height_mm <= 3.0 and 1.0 <= epsilon_r <= 15.0


# -- reading the board -----------------------------------------------------


def copper_thickness(board: Any, layer: str) -> tuple[float, str]:
    """Copper thickness for a layer in mm, and where the number came from."""
    for entry in board.stackup:
        if entry.get("name") == layer and entry.get("thickness"):
            return float(entry["thickness"]), "stackup"
    return DEFAULT_COPPER_THICKNESS_MM, "assumed 1 oz (no stackup in the board)"


def _copper_layers_in_stack(board: Any) -> list[int]:
    return [i for i, entry in enumerate(board.stackup) if entry.get("type") == "copper"]


def layer_geometry(board: Any, layer: str) -> dict[str, Any] | None:
    """The dielectric a layer sits against, and hence which model applies.

    Outer layers see one reference plane through the adjacent dielectric, so they
    are microstrip. Inner layers sit between two, so they are stripline and the
    height that matters is the plane-to-plane spacing.
    """
    coppers = _copper_layers_in_stack(board)
    if len(coppers) < 2:
        return None
    try:
        index = next(i for i in coppers if board.stackup[i].get("name") == layer)
    except StopIteration:
        return None

    position = coppers.index(index)
    outer = position in (0, len(coppers) - 1)
    neighbour = coppers[1] if position == 0 else coppers[position - 1]
    low, high = sorted((index, neighbour))
    dielectrics = [
        entry
        for entry in board.stackup[low + 1 : high]
        if entry.get("thickness") and entry.get("type") != "copper"
    ]
    if not dielectrics:
        return None
    height = sum(float(entry["thickness"]) for entry in dielectrics)
    epsilons = [float(e["epsilon_r"]) for e in dielectrics if e.get("epsilon_r")]
    if not epsilons:
        return None

    geometry = {
        "layer": layer,
        "kind": "microstrip" if outer else "stripline",
        "height_mm": round(height, 4),
    }
    if not outer:
        # Between two planes: the model wants the whole gap, not half of it -
        # and the solver wants to know where in the gap the trace sits, which
        # the fit cannot ask.
        above = coppers[position - 1]
        below = coppers[position + 1]
        span = [
            entry
            for entry in board.stackup[above + 1 : below]
            if entry.get("thickness") and entry.get("type") != "copper"
        ]
        if span:
            height = sum(float(entry["thickness"]) for entry in span)
            epsilons = [float(e["epsilon_r"]) for e in span if e.get("epsilon_r")] or epsilons
            geometry["height_mm"] = round(height, 4)
            geometry["height_below_mm"] = round(
                sum(
                    float(entry["thickness"])
                    for entry in board.stackup[index + 1 : below]
                    if entry.get("thickness") and entry.get("type") != "copper"
                ),
                4,
            )

    geometry["epsilon_r"] = round(sum(epsilons) / len(epsilons), 3)
    return geometry


def analyse(board: Any, *, temperature_rise_c: float = 10.0, solve: bool = False) -> dict[str, Any]:
    """Per-net copper properties, plus what this stackup needs for 50/90/100 ohm.

    ``solve`` re-measures the closed forms' answers with the 2D field solver in
    `field2d`: the 50 ohm width is solved as the cross-section it actually is,
    and the 100 ohm differential pair as two coupled traces rather than an
    exponential correction factor. It costs a few seconds per layer, which is
    why it is a flag and not the default.
    """
    thickness_cache: dict[str, tuple[float, str]] = {}

    def thickness_of(layer: str) -> tuple[float, str]:
        if layer not in thickness_cache:
            thickness_cache[layer] = copper_thickness(board, layer)
        return thickness_cache[layer]

    outer = {board.copper_layers[0], board.copper_layers[-1]} if board.copper_layers else set()

    per_net: dict[str, dict[str, Any]] = {}
    for track in board.tracks:
        if not track.net or track.width <= 0:
            continue
        length = math.dist(track.start, track.end)
        if length <= 0:
            continue
        thickness, source = thickness_of(track.layer)
        entry = per_net.setdefault(
            track.net,
            {
                "net": track.net,
                "length_mm": 0.0,
                "resistance_mohm": 0.0,
                "narrowest_mm": track.width,
                "narrowest_layer": track.layer,
                "layers": set(),
                "thickness_source": source,
            },
        )
        entry["length_mm"] += length
        entry["resistance_mohm"] += track_resistance(length, track.width, thickness) * 1000.0
        entry["layers"].add(track.layer)
        if track.width < entry["narrowest_mm"]:
            entry["narrowest_mm"] = track.width
            entry["narrowest_layer"] = track.layer

    nets = []
    for entry in per_net.values():
        thickness, _ = thickness_of(entry["narrowest_layer"])
        is_external = entry["narrowest_layer"] in outer
        nets.append(
            {
                "net": entry["net"],
                "length_mm": round(entry["length_mm"], 3),
                # Every segment in series: an upper bound on the resistance
                # between any two points, since parallel paths only lower it.
                "resistance_mohm": round(entry["resistance_mohm"], 3),
                "narrowest_mm": entry["narrowest_mm"],
                "narrowest_layer": entry["narrowest_layer"],
                "narrowest_is_external": is_external,
                "current_a": round(
                    current_capacity(
                        entry["narrowest_mm"],
                        thickness,
                        temperature_rise_c=temperature_rise_c,
                        external=is_external,
                    ),
                    3,
                ),
                "layers": sorted(entry["layers"]),
                "copper_thickness_mm": thickness,
                "thickness_source": entry["thickness_source"],
            }
        )
    nets.sort(key=lambda row: row["current_a"])

    impedance = []
    for layer in board.copper_layers:
        geometry = layer_geometry(board, layer)
        if not geometry:
            continue
        thickness, _ = thickness_of(layer)
        kind = geometry["kind"]
        height, epsilon = geometry["height_mm"], geometry["epsilon_r"]
        row = {**geometry, "copper_thickness_mm": thickness}
        for target, key in ((50.0, "width_50r_mm"), (75.0, "width_75r_mm")):
            width = width_for_impedance(target, thickness, height, epsilon, kind=kind)
            row[key] = round(width, 4) if width else None
        for target, key in ((90.0, "width_90r_diff_mm"), (100.0, "width_100r_diff_mm")):
            width = width_for_differential_impedance(target, thickness, height, epsilon, kind=kind)
            row[key] = round(width, 4) if width else None
        if solve:
            from . import field2d

            if kind == "microstrip":
                if row.get("width_50r_mm"):
                    solved = field2d.microstrip(row["width_50r_mm"], thickness, height, epsilon)
                    row["width_50r_solved_ohm"] = solved["z0_ohm"]
                    row["solved_eps_eff"] = solved["eps_eff"]
                if row.get("width_100r_diff_mm"):
                    pair = field2d.differential_microstrip(
                        row["width_100r_diff_mm"],
                        thickness,
                        height,
                        epsilon,
                        row["width_100r_diff_mm"],
                    )
                    row["width_100r_diff_solved_ohm"] = pair["zdiff_ohm"]
            elif row.get("width_50r_mm"):
                # the inner layers get the referee too: the IPC stripline fit
                # proposed the width, the solve re-measures it - at the
                # trace's real position in the gap, which an asymmetric
                # stackup states and the symmetric fit cannot pose (the
                # coupled stripline pair has no solver yet, so that
                # differential column stays a fit)
                solved = field2d.stripline(
                    row["width_50r_mm"],
                    thickness,
                    height,
                    epsilon,
                    trace_below_mm=row.get("height_below_mm"),
                )
                row["width_50r_solved_ohm"] = solved["z0_ohm"]
        impedance.append(row)

    return {
        "temperature_rise_c": temperature_rise_c,
        "nets": nets,
        "impedance": impedance,
        "notes": [
            "current_a is IPC-2221 for the net's narrowest segment in still air.",
            "resistance_mohm sums every segment: an upper bound on the resistance "
            "between any two points on the net, since parallel paths only lower it.",
            "Microstrip widths are Hammerstad-Jensen with its thickness correction, "
            "worth about +-2% against a field solve; stripline is the IPC-2141 fit at "
            "about +-10%. The differential gap is taken equal to the width. Neither "
            "model knows your laminate's real permittivity: confirm with your fab.",
        ]
        + (
            [
                "width_*_solved_ohm re-measures those widths with the 2D field "
                "solver: the closed form proposed the width, the solve checked it."
            ]
            if solve
            else []
        ),
    }
