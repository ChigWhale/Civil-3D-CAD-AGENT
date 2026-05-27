import math
import numpy as np

TOLERANCE = 1e-6


# =============================================================================
# 2.4 — Parallel Lines
#
# Lines P1P2 and P3P4 must be parallel.
# F = (E2-E1)(N4-N3) - (N2-N1)(E4-E3) = 0
#
# P1, P2 are fixed (reference line).
# P3, P4 are free (new line being solved).
# =============================================================================

def _parallel_residual(x, E1, N1, E2, N2):
    """
    Residual for the parallel constraint with line 1 fixed.
    x = [E3, N3, E4, N4]
    """
    E3, N3, E4, N4 = x
    return [(E2 - E1) * (N4 - N3) - (N2 - N1) * (E4 - E3)]


def _parallel_jacobian(x, E1, N1, E2, N2):
    """
    Jacobian of the parallel residual w.r.t. [E3, N3, E4, N4].

    F = (E2-E1)(N4-N3) - (N2-N1)(E4-E3)

    dF/dE3 =  (N2-N1)
    dF/dN3 = -(E2-E1)
    dF/dE4 = -(N2-N1)
    dF/dN4 =  (E2-E1)
    """
    dE12 = E2 - E1
    dN12 = N2 - N1
    return np.array([[dN12, -dE12, -dN12, dE12]])


def solve_parallel(line1, line2):
    """
    Adjust line2 so it is parallel to line1.

    Parameters
    ----------
    line1 : [E1, N1, E2, N2]  — fixed reference line
    line2 : [E3, N3, E4, N4]  — initial coordinates of the new line

    Returns
    -------
    dict with keys:
        "solved"  : bool   — True if constraint is satisfied within tolerance
        "line2"   : [E3, N3, E4, N4] — adjusted coordinates
        "residual": float  — final constraint residual (should be near 0)
        "message" : str    — solver status
    """
    E1, N1, E2, N2 = [float(v) for v in line1]
    x = np.array([float(v) for v in line2])

    for _ in range(50):
        F  = np.array(_parallel_residual(x, E1, N1, E2, N2))
        if np.abs(F).max() < TOLERANCE:
            break
        J  = _parallel_jacobian(x, E1, N1, E2, N2)
        dx = np.linalg.lstsq(J, -F, rcond=None)[0]
        x += dx

    final_residual = abs(_parallel_residual(x, E1, N1, E2, N2)[0])
    solved = final_residual < TOLERANCE

    return {
        "solved":   solved,
        "line2":    x.tolist(),
        "residual": final_residual,
        "message":  "Parallel constraint satisfied." if solved
                    else f"Solver did not converge. Residual: {final_residual:.6e}",
    }


# =============================================================================
# Offset — Parallel + perpendicular displacement
#
# Decomposes into:
#   1. Compute offset position directly from reference line normal
#   2. Verify with parallel constraint solver
# =============================================================================

def solve_offset(line1, distance, side="left"):
    """
    Create a new line offset from line1 by a perpendicular distance.

    Parameters
    ----------
    line1    : [E1, N1, E2, N2]  — fixed reference line
    distance : float             — perpendicular offset distance
    side     : "left" | "right"  — offset direction relative to line direction

    Returns
    -------
    dict with keys:
        "solved"  : bool
        "line2"   : [E3, N3, E4, N4] — offset line coordinates
        "residual": float
        "message" : str
    """
    E1, N1, E2, N2 = [float(v) for v in line1]

    dE     = E2 - E1
    dN     = N2 - N1
    length = math.sqrt(dE ** 2 + dN ** 2)

    if length < TOLERANCE:
        return {
            "solved":   False,
            "line2":    [E1, N1, E2, N2],
            "residual": None,
            "message":  "Reference line has zero length — cannot compute offset.",
        }

    # Left-hand unit normal: rotate direction 90° CCW → (-dN, dE) / length
    ne = -dN / length
    nn =  dE / length

    if side == "right":
        ne, nn = -ne, -nn

    # Shift both endpoints of line1 by distance along normal
    E3 = E1 + distance * ne
    N3 = N1 + distance * nn
    E4 = E2 + distance * ne
    N4 = N2 + distance * nn

    # Verify parallel constraint is satisfied
    return solve_parallel(line1, [E3, N3, E4, N4])
