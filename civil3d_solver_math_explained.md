# Civil 3D Geometric Solver — Math Explained

A plain-language walkthrough of the math behind every constraint in the solver.
Each section mirrors the numbering in the constraint reference document.

---

## 1. Point & Distance Constraints

### 1.1 — Fixed Point

**What it does:** Pins a point to a known location.

**The math:** This is the simplest possible constraint. If you want point P
to sit at coordinates (500, 1000), you just say "the easting of P minus 500
must equal zero" and "the northing of P minus 1000 must equal zero." Two
equations, two unknowns consumed. The Jacobian is trivial — each equation
depends on exactly one variable with a derivative of 1.

**Why you need it:** Every solver needs at least some fixed points to anchor
the geometry. Without them, the entire solution can translate or rotate
freely, making the Jacobian singular.

---

### 1.2 — Point-to-Point Distance

**What it does:** Forces two points to be exactly d apart.

**The math:** The Euclidean distance formula is sqrt((E₂-E₁)² + (N₂-N₁)²) = d.
But square roots cause problems in solvers — they're undefined at zero and
their derivatives blow up near zero. So we square both sides:

    (E₂-E₁)² + (N₂-N₁)² - d² = 0

This is algebraically equivalent but much better behaved numerically. The
partial derivatives fall out directly from the power rule — differentiating
(E₂-E₁)² with respect to E₁ gives -2(E₂-E₁) by the chain rule.

**Geometric intuition:** This constraint defines a circle of radius d
centered on one point. The other point must lie somewhere on that circle.
You need additional constraints (bearing, another distance) to pin down
exactly where.

---

### 1.3 — Point-on-Line

**What it does:** Forces a point to lie on a given line.

**The math:** A line through point A with direction vector v = (vₑ, vₙ) can
be described parametrically as A + t·v. A point P lies on this line if and
only if the vector (P - A) is parallel to v. Two vectors are parallel when
their cross product is zero:

    (P - A) × v = (E_P - E_A)·vₙ - (N_P - N_A)·vₑ = 0

The cross product in 2D gives a scalar. Geometrically, this scalar is the
signed perpendicular distance from P to the line (scaled by |v|). Setting
it to zero means "P has zero distance from the line," i.e., P is on the line.

**Why cross product instead of parametric form:** The parametric form
P = A + t·v introduces an extra unknown (the parameter t). The cross product
formulation eliminates t entirely, giving one clean equation.

---

### 1.4 — Point-on-Circle

**What it does:** Forces a point to lie on a circle.

**The math:** Identical in structure to the distance constraint (1.2). A circle
is defined as "all points at distance R from center C." So:

    (E_P - E_C)² + (N_P - N_C)² - R² = 0

Again, we use the squared form to avoid the square root. This is one equation
constraining two unknowns (the point's coordinates), so the point can still
slide along the circle — you need another constraint to fix its position.

---

### 1.5 — Coincident Points

**What it does:** Makes two points occupy the same location.

**The math:** Simply E₂ - E₁ = 0 and N₂ - N₁ = 0. This is a degenerate
case of the distance constraint with d = 0. We use the linear form rather
than the squared distance form because the squared form has a zero gradient
at the solution point, which would make the Jacobian singular and break
Newton-Raphson convergence.

---

## 2. Angular & Direction Constraints

### 2.1 — Fixed Bearing / Azimuth

**What it does:** Forces a line segment to point in a specific compass direction.

**The math:** In surveying, azimuth (bearing) is measured clockwise from north.
The atan2 function computes the angle of a vector from its components. Since
north corresponds to the positive N-axis and east to the positive E-axis,
the azimuth from P₁ to P₂ is:

    θ = atan2(E₂ - E₁, N₂ - N₁)

Note the argument order: atan2(ΔE, ΔN), not atan2(ΔN, ΔE). This is because
we want the angle from the N-axis (north), not from the E-axis. In standard
math, atan2(y, x) gives the angle from the x-axis. Here, north plays the
role of the x-axis.

The Jacobian terms come from differentiating atan2. For atan2(a, b), the
derivative with respect to a is b/(a²+b²) and with respect to b is
-a/(a²+b²). Substituting a = ΔE, b = ΔN and applying the chain rule gives
the partials listed in the reference.

**Watch out:** atan2 has a branch cut at ±π (due south). If your bearing is
near 180°, the residual can jump from +π to -π across a single Newton step.
Always normalize the residual to (-π, π] after computing it.

---

### 2.2 — Angle Between Three Points

**What it does:** Forces the angle at a vertex B (formed by rays BA and BC)
to equal a specified value.

**The math:** Form vectors u = A - B and w = C - B (pointing from the vertex
toward each ray endpoint). The angle between two vectors can be computed
from their dot product and cross product:

    dot product:   u · w = |u||w| cos(α)
    cross product: u × w = |u||w| sin(α)

Therefore:
    α = atan2(u × w, u · w)

This gives the signed angle from u to w. The atan2 formulation is preferred
over acos(u·w / |u||w|) because acos has poor numerical behavior near 0°
and 180° (its derivative goes to infinity), while atan2 is well-conditioned
everywhere except at the origin (|u| = 0 or |w| = 0, which would mean B
coincides with A or C — a degenerate case).

---

### 2.3 — Deflection Angle

**What it does:** At a PI (point of intersection) in an alignment, the
change in direction between the incoming and outgoing tangent must equal Δ.

**The math:** This is the same atan2-based angle calculation as 2.2, but
applied to direction vectors rather than position vectors. The incoming
direction is d_in = PI - prev, the outgoing direction is d_out = next - PI.
The deflection angle is the angle you'd have to turn through to go from
the incoming bearing to the outgoing bearing:

    Δ = atan2(d_in × d_out, d_in · d_out)

Positive Δ means a right turn, negative means a left turn (or vice versa,
depending on your sign convention — be consistent).

**Civil 3D context:** The deflection angle is the fundamental parameter that
ties an alignment's tangent geometry to its curve geometry. Every curve
(circular or spiral) exists to traverse some portion of this deflection.

---

### 2.4 — Parallel Lines

**What it does:** Forces two line segments to have the same direction.

**The math:** Two vectors are parallel if and only if their cross product is
zero. For lines P₁P₂ and P₃P₄:

    (E₂ - E₁)(N₄ - N₃) - (N₂ - N₁)(E₄ - E₃) = 0

This is saying "the sine of the angle between them is zero," which happens
at 0° and 180°. This means the constraint accepts both same-direction and
opposite-direction parallelism. If you need same-direction only, add a
check that the dot product is positive (but handle this outside the solver
as a validation, not as a Newton-Raphson equation).

---

### 2.5 — Perpendicular Lines

**What it does:** Forces two line segments to meet at 90°.

**The math:** Two vectors are perpendicular if and only if their dot product
is zero:

    (E₂ - E₁)(E₄ - E₃) + (N₂ - N₁)(N₄ - N₃) = 0

This is saying "the cosine of the angle between them is zero," which happens
at 90° and 270°. The dot product is the natural complement of the cross
product used for parallelism — one measures the cosine component, the other
measures the sine component.

---

## 3. Circular Curve Constraints

### 3.1 — Radius

**What it does:** Fixes the curve radius to a design value.

**The math:** Simply R - R_target = 0. The radius might be a free variable
in the solver (if you're solving for the best-fit radius), or it might be
a fixed input. When it's fixed, this constraint just pins it. The Jacobian
entry is ∂F/∂R = 1.

---

### 3.2 & 3.3 — PC on Back Tangent / PT on Ahead Tangent

**What they do:** Ensure the curve's start (PC) and end (PT) points lie on
their respective tangent lines.

**The math:** These are instances of the point-on-line constraint (1.3).
The PC must lie on the line through the PI in the direction of the incoming
tangent, and the PT must lie on the line through the PI in the direction of
the outgoing tangent.

**Why this matters:** A circular curve doesn't just float in space — it must
connect to the tangent lines at precisely the right points. These constraints
anchor the curve to the alignment's straight segments.

---

### 3.4 — Tangent Length (T)

**What it does:** Ensures the distance from the PI to the PC (or PT) equals
the theoretical tangent length.

**The math:** For a circular curve with radius R and deflection angle Δ,
basic trigonometry gives the tangent length. If you draw the triangle formed
by the PI, the curve center, and the PC:

- The angle at the PI is Δ/2 (half the deflection)
- The side from center to PC is R (the radius)
- The side from PI to PC is the tangent length T

Since tan(Δ/2) = opposite/adjacent and the radius is the opposite side
(perpendicular to the tangent) while T is the adjacent side:

    T = R · tan(Δ/2)

The constraint checks that the actual measured distance from PI to PC
matches this formula.

---

### 3.5 & 3.6 — PC Tangency / PT Tangency

**What they do:** Ensure the curve is tangent (smooth, no kink) at the PC
and PT.

**The math:** A circle is tangent to a line at a point if and only if the
radius to that point is perpendicular to the line. The perpendicularity
test is the dot product:

    (PC - Center) · (tangent direction) = 0

This is the dot product of the radius vector with the tangent direction
vector. If it's zero, the two are perpendicular, which means the curve
"kisses" the tangent line at that point without crossing it (locally).

**Why this matters:** Without tangency constraints, the curve could meet the
tangent line at an angle, creating an abrupt direction change — physically
impossible for a road or pipe.

---

### 3.7 — Center Equidistant from PC and PT

**What it does:** Ensures the curve center is the same distance from both
endpoints of the arc.

**The math:** Both PC and PT lie on the circle, so both must be at distance
R from the center. Rather than writing two separate point-on-circle
equations (which would be redundant with 3.5/3.6), we write the
single equation:

    |PC - C|² - |PT - C|² = 0

Expanding:
    (E_PC - E_C)² + (N_PC - N_C)² - (E_PT - E_C)² - (N_PT - N_C)² = 0

This difference-of-squares form avoids introducing R explicitly and is
linear in the center coordinates (the squared terms of E_C and N_C cancel),
which is numerically nice.

---

### 3.8 — Arc Length

**What it does:** Fixes the curve's arc length.

**The math:** The arc length of a circular arc is the radius times the
central angle (in radians):

    L = R · Δ

This comes directly from the definition of a radian — one radian is the
angle that subtends an arc equal in length to the radius. So Δ radians
subtends an arc of R·Δ.

---

### 3.9 — Chord Length

**What it does:** Fixes the straight-line distance from PC to PT.

**The math:** Drop a perpendicular from the center to the chord, bisecting
both the chord and the central angle. In the resulting right triangle:

    sin(Δ/2) = (half chord) / R

Therefore:
    full chord = 2R · sin(Δ/2)

---

### 3.10 — Mid-Ordinate (M)

**What it does:** Fixes the distance from the chord midpoint to the arc
midpoint.

**The math:** The center is at distance R from the arc. The center is at
distance R·cos(Δ/2) from the chord midpoint (using the same right triangle
as 3.9). The mid-ordinate is the difference:

    M = R - R·cos(Δ/2) = R·(1 - cos(Δ/2))

**Physical meaning:** This is how far the curve "bulges" away from the
straight line between PC and PT. Highway designers use it for clearance
calculations (sight lines past barriers, etc.).

---

### 3.11 — External Distance (E)

**What it does:** Fixes the distance from the PI to the curve midpoint.

**The math:** The PI is at distance R/cos(Δ/2) from the center (from the
tangent-length triangle). The curve midpoint is at distance R from the
center. The external distance is:

    E = R/cos(Δ/2) - R = R·(sec(Δ/2) - 1)

where sec = 1/cos. This is the distance from the PI inward to the nearest
point on the curve.

---

## 4. Spiral (Clothoid) Constraints

Spirals are transition curves whose curvature changes linearly with arc
length. They're used between tangent sections (κ = 0) and circular arcs
(κ = 1/R) to provide a smooth curvature transition, which is critical for
vehicle dynamics at highway speeds.

### 4.1 — Spiral Parameter

**What it does:** Ties the spiral parameter A to the radius and length.

**The math:** The defining property of a clothoid is that curvature κ is
proportional to arc length s:

    κ(s) = s / A²

At the end of the spiral (s = L_s), the curvature must equal 1/R:

    L_s / A² = 1/R
    A² = R · L_s

A is the "flatness parameter" — larger A means a gentler transition.

---

### 4.2 — Spiral Deflection Angle

**What it does:** Computes how much direction change the spiral consumes.

**The math:** Since curvature is the rate of change of direction with respect
to arc length (κ = dθ/ds), integrate to get total direction change:

    θ_s = ∫₀^{L_s} κ(s) ds = ∫₀^{L_s} (s/A²) ds = L_s² / (2A²)

Substituting A² = R·L_s:

    θ_s = L_s² / (2·R·L_s) = L_s / (2R)

So the spiral consumes exactly half the deflection that a circular arc of
the same length and radius would consume. This factor of 2 is fundamental
to clothoid geometry.

---

### 4.3 — Spiral Tangent Offset (X, Y)

**What it does:** Computes where the end of the spiral lands in local
coordinates.

**The math:** Starting from the spiral's origin (the TS point), with the
initial tangent along the local X-axis, the position at arc length s is:

    x(s) = ∫₀ˢ cos(θ(u)) du
    y(s) = ∫₀ˢ sin(θ(u)) du

where θ(u) = u²/(2A²). These are Fresnel integrals, which have no
closed-form solution. Instead, we use the Taylor series expansions of
cos and sin:

    cos(θ) = 1 - θ²/2 + θ⁴/24 - ...
    sin(θ) = θ - θ³/6 + θ⁵/120 - ...

Integrating term by term (with θ = u²/(2A²) substituted and evaluated at
u = L_s) gives the power series shown in the reference:

    X_s = L_s · [1 - θ_s²/10 + θ_s⁴/216 - ...]
    Y_s = L_s · [θ_s/3 - θ_s³/42 + θ_s⁵/1320 - ...]

The denominators (10, 216, 3, 42, 1320) come from the combined factorials
and powers in the integration. Five terms give sub-millimeter accuracy for
typical highway spirals.

---

### 4.4 — Spiral Tangent Distance (T_s)

**What it does:** Computes the distance from the PI to the TS (tangent-to-
spiral point).

**The math:** The spiral displaces the circular arc inward and along the
tangent relative to a simple tangent-to-curve arrangement. Two auxiliary
values capture this displacement:

    p = Y_s - R·(1 - cos(θ_s))     [inward shift]
    k = X_s - R·sin(θ_s)           [along-tangent shift]

p is how much the spiral pushes the circular arc toward the tangent line.
k is how much the spiral pushes the PC back along the tangent.

The total tangent distance (from PI to TS) is then:

    T_s = k + (R + p) · tan(Δ/2)

This is analogous to the simple tangent length T = R·tan(Δ/2) but adjusted
for the spiral's displacement. The term (R + p)·tan(Δ/2) replaces
R·tan(Δ/2), and k adds the extra distance from the spiral's along-tangent
extent.

---

### 4.5 — Total Deflection (SCS)

**What it does:** Ensures all the deflection is accounted for.

**The math:** The total deflection Δ at a PI must be consumed by the
combination of spirals and circular arc:

    Δ = Δ_c + θ_s_in + θ_s_out

where Δ_c is the central angle of the circular arc portion, and θ_s_in,
θ_s_out are the deflection angles of the entry and exit spirals. This is
simply conservation of angle — the total direction change through the
curve complex must equal the deflection between the tangent lines.

---

## 5. Profile / Vertical Curve Constraints

Profile geometry uses station (S) as the horizontal axis and elevation (Z)
as the vertical axis. Vertical curves are parabolas, not circular arcs,
because a parabola provides a constant rate of change of grade — which
corresponds to constant vertical acceleration for a vehicle at constant speed.

### 5.1 — Grade (Slope)

**What it does:** Fixes the slope between two points.

**The math:** Grade is rise over run:

    g = (Z₂ - Z₁) / (S₂ - S₁)

Often expressed as a percentage (e.g., 3% = 0.03). The constraint sets
this ratio equal to a target. This is a linear equation in Z₁ and Z₂
(if stations are fixed), making it very well-behaved for Newton-Raphson.

---

### 5.2 — Fixed Elevation

**What it does:** Pins an elevation to a known value.

**The math:** Z - Z_target = 0. Same structure as the fixed-point constraint
but in one dimension. Used for tie-ins to existing infrastructure (matching
an existing road surface, a bridge deck elevation, etc.).

---

### 5.3 — Vertical Curve Length

**What it does:** Fixes the length of a vertical curve.

**The math:** The length of a vertical curve is measured along the horizontal
(station) axis, not along the curve itself. This is because vertical curves
have such small deflections that the difference between horizontal and
slope distance is negligible. L = S_EVC - S_BVC.

---

### 5.4 — K-Value (Rate of Curvature)

**What it does:** Controls how gently the vertical curve transitions between
grades.

**The math:** K is defined as the horizontal distance needed to achieve a 1%
change in grade:

    K = L / |g₂ - g₁|

where g₁ and g₂ are in percent. A larger K means a longer, flatter curve.
Design standards specify minimum K values for crest curves (based on
stopping sight distance) and sag curves (based on headlight illumination
and comfort).

---

### 5.5 — Parabolic Curve Elevation

**What it does:** Computes the elevation at any point along a vertical curve.

**The math:** A symmetric parabolic vertical curve has the equation:

    Z(x) = Z_BVC + g₁·x + (r/2)·x²

where x = S - S_BVC is the distance from the beginning of the vertical
curve, and r is the rate of change of grade:

    r = (g₂ - g₁) / L

This is a standard quadratic. At x = 0, Z = Z_BVC and the slope is g₁.
At x = L, the slope is g₁ + r·L = g₁ + (g₂ - g₁) = g₂. So the parabola
smoothly transitions from grade g₁ to grade g₂.

The term (r/2)·x² is the vertical offset from the tangent grade line.
Engineers call this the "y" or "tangent offset" — it's how far the curve
departs from a straight extension of the entry grade.

---

### 5.6 — High/Low Point Station

**What it does:** Locates the crest or sag of a vertical curve.

**The math:** The high or low point occurs where the slope is zero. Taking
the derivative of Z(x):

    dZ/dx = g₁ + r·x = 0
    x_turn = -g₁/r = -g₁·L / (g₂ - g₁)

This only produces a valid point within the curve when g₁ and g₂ have
opposite signs (for a sag curve with g₁ < 0, g₂ > 0) or the same sign
but the curve is long enough. If x_turn is outside [0, L], the extreme
point is at one of the endpoints.

**Why it matters:** The high point of a crest curve determines sight distance.
The low point of a sag curve determines drainage — water collects there.

---

### 5.7 — Minimum Cover / Clearance

**What it does:** Ensures the profile maintains a minimum vertical separation
from a reference surface (e.g., existing ground for buried utilities, or an
overhead structure for clearance).

**The math:** As an equality: Z_profile - Z_surface - cover_min = 0.

For an inequality (Z_profile - Z_surface ≥ cover_min), we introduce a slack
variable s and write:

    Z_profile - Z_surface - cover_min - s² = 0

The s² term is always non-negative, so this equation can only be satisfied
when the cover is at least cover_min. The solver finds both the profile
elevation and the slack variable. If s converges to zero, the constraint is
"active" (binding). If s converges to a positive value, there's excess cover.

---

### 5.8 — Sight Distance (Crest)

**What it does:** Ensures a crest vertical curve is long enough for a driver
to see an obstacle in time to stop.

**The math:** The AASHTO stopping sight distance formula for crest curves
(when the sight distance is less than the curve length) is:

    L = S_sd² · |g₂ - g₁| / (200 · (√h₁ + √h₂)²)

where S_sd is the required stopping sight distance, h₁ is the driver's eye
height (typically 1.08m / 3.5ft), and h₂ is the object height (typically
0.6m / 2.0ft).

The derivation comes from the geometry of two sight lines: one from the
driver's eye to the crest of the curve, and one from the object to the
crest. The parabolic profile determines how far each can "see" over the
hump, and the formula ensures the total sight line reaches at least S_sd.

The 200 in the denominator comes from converting the grade difference from
percent to decimal (factor of 100) and the factor of 2 from the parabola's
equation.

---

## 6. Tangency & Continuity Constraints

These ensure smooth connections between adjacent geometric elements.
The three levels (G0, G1, G2) form a hierarchy — each includes the previous.

### 6.1 — G0 (Positional) Continuity

**What it does:** Two segments share an endpoint — no gap.

**The math:** The end of segment 1 must have the same coordinates as the
start of segment 2. Two equations (ΔE = 0, ΔN = 0). This is identical to
the coincident-points constraint (1.5).

**What it doesn't do:** G0 alone allows a sharp corner (kink) at the
junction. The path is connected but not smooth.

---

### 6.2 — G1 (Tangent) Continuity

**What it does:** The direction is continuous across the junction — no kink.

**The math:** Compute the tangent direction vectors at the junction from
each side. For a line, the tangent is just the line's direction vector. For
a circle, the tangent is perpendicular to the radius at that point. For a
spiral, the tangent direction comes from the clothoid equations.

The cross product of the two tangent vectors must be zero:

    t₁ × t₂ = t₁ₑ · t₂ₙ - t₁ₙ · t₂ₑ = 0

This ensures they're parallel. We also need them to point the same way (not
opposite), which we verify with t₁ · t₂ > 0. This sign check is handled
outside the equation system as a validation.

**Physical meaning:** A vehicle can traverse a G1 junction without a
discontinuous steering input. The path is smooth but the curvature may jump.

---

### 6.3 — G2 (Curvature) Continuity

**What it does:** The curvature is continuous across the junction — no sudden
change in steering.

**The math:** Curvature κ is the reciprocal of the radius of the osculating
(best-fit) circle at a point:

- For a straight line: κ = 0
- For a circular arc of radius R: κ = 1/R (constant)
- For a spiral at distance s from its start: κ = s/A²

The constraint sets the curvatures equal at the junction:

    κ₁_end - κ₂_start = 0

**Why spirals exist:** Without spirals, a tangent-to-curve transition goes
from κ = 0 to κ = 1/R instantaneously — a curvature discontinuity. This
means a driver would have to jerk the steering wheel instantly at the PC.
A spiral provides G2 continuity, with curvature changing linearly from 0
to 1/R over the spiral length.

---

## 7. Offset & Corridor Constraints

These handle the cross-section dimension — everything measured perpendicular
to the alignment.

### 7.1 — Horizontal Offset

**What it does:** Places a point at a specified perpendicular distance from
the alignment.

**The math:** At any station S along the alignment, you can compute the
alignment's position A(S) and its unit tangent vector t(S). The left-hand
normal is n = (-tₙ, tₑ) (rotate the tangent 90° counterclockwise). A point
at offset d is:

    P = A(S) + d · n(S)

Positive d is to the left, negative to the right (by convention). The
constraint enforces this relationship.

**Subtlety:** The tangent direction (and therefore the normal) changes along
curves. So the offset direction rotates as you move along the alignment.
If S is an unknown in the solver, the Jacobian must include the derivatives
of t(S) and A(S) with respect to S, which depend on the alignment geometry.

---

### 7.2 — Cross Slope

**What it does:** Sets the transverse slope of a surface (like a lane) to
a specified value.

**The math:** Cross slope is the elevation difference divided by the
horizontal offset difference:

    m = (Z₂ - Z₁) / (d₂ - d₁)

where d₁, d₂ are the offsets from centerline and Z₁, Z₂ are the elevations
at those offsets. This is simply the rise/run in the cross-section view.

**Typical values:** Normal crown is 2% (0.02). Superelevation can go up to
8% (0.08) or more.

---

### 7.3 — Lane Width

**What it does:** Fixes the horizontal distance between two offset lines.

**The math:** |d_right - d_left| - w = 0. This is a one-dimensional
distance constraint on offset values.

---

### 7.4 — Superelevation

**What it does:** At a specific station, the cross slope must match a
designed superelevation value.

**The math:** Same as cross slope (7.2) but the target value e(S) varies
with station according to a superelevation schedule. On tangent sections,
e is the normal crown. Through spiral transitions, e ramps from crown to
full super. On circular curves, e is constant at the design value.

**The constraint:**
    (Z_edge - Z_CL) / offset - e(S) = 0

The superelevation function e(S) is typically piecewise linear or
piecewise parabolic, defined by the designer in a superelevation table.

---

### 7.5 — Daylight (Cut/Fill Slope Intercept)

**What it does:** Finds where a designed slope from the edge of pavement
intersects the existing ground surface.

**The math:** Starting from the edge elevation Z_edge at offset d_edge, a
slope of 1:m (one unit vertical per m units horizontal) extends outward.
The designed surface elevation at offset d is:

    Z_design(d) = Z_edge + (d - d_edge) / m     [for a cut slope]
    Z_design(d) = Z_edge - (d - d_edge) / m     [for a fill slope]

The daylight point is where this equals the existing ground:

    Z_design(d) = Z_ground(d)

The tricky part is that Z_ground(d) is not a simple formula — it comes from
a terrain model (TIN surface). The solver must evaluate the TIN at each
candidate offset d, making this a nonlinear root-finding problem even for
a single cross-section. Newton-Raphson works well here with d as the unknown.

---

## 8. Pipe Network Constraints

### 8.1 — Invert Elevation Match

**What it does:** Ensures a pipe connects to a structure (manhole, inlet)
at the right elevation.

**The math:** Z_pipe_invert - Z_structure_invert = 0. Direct equality.
The pipe's invert at the structure wall must match the structure's shelf or
channel elevation. In practice, different pipes may connect at different
elevations within the same structure, so each pipe-structure connection gets
its own constraint.

---

### 8.2 — Minimum Pipe Slope

**What it does:** Ensures the pipe has enough slope for gravity flow.

**The math:** Slope = (Z_upstream_invert - Z_downstream_invert) / pipe_length.
For a minimum slope requirement:

    slope - slope_min ≥ 0

Converted to an equality with a slack variable:

    slope - slope_min - s² = 0

Minimum slopes are typically set by pipe size and material — enough to
maintain a self-cleansing velocity (usually around 2 ft/s or 0.6 m/s).

---

### 8.3 — Minimum Cover

**What it does:** Ensures the pipe is deep enough below the ground surface.

**The math:** The top of the pipe (crown) is at:

    Z_crown = Z_invert + D_pipe

where D_pipe is the outside diameter. The cover is:

    cover = Z_ground - Z_crown

The constraint: cover ≥ cover_min, or:

    Z_ground - Z_invert - D_pipe - cover_min - s² = 0

Cover requirements depend on the pipe material, traffic loads, and frost
depth — typically 3-5 feet for sanitary and storm sewers.

---

### 8.4 — Hydraulic Capacity (Manning's)

**What it does:** Ensures the pipe can carry the required flow rate.

**The math:** Manning's equation for open-channel/gravity-pipe flow:

    Q = (1/n) · A · R^(2/3) · S^(1/2)

where n is the roughness coefficient (depends on pipe material — 0.013 for
concrete, 0.009 for HDPE), A is the flow cross-sectional area, R is the
hydraulic radius (R = A / wetted perimeter), and S is the pipe slope.

For a full circular pipe: A = π·D²/4, wetted perimeter = π·D, R = D/4.
For partially full pipes, A and R are functions of the depth/diameter ratio,
which involves trigonometry (circular segment geometry).

The constraint Q_computed - Q_required = 0 is nonlinear in the pipe
diameter and slope, both of which may be unknowns.

---

### 8.5 — Structure Rim Elevation

**What it does:** Matches the top of a structure (manhole rim, inlet grate)
to the ground surface.

**The math:** Z_rim - Z_ground = 0. The rim must sit flush with the ground.
Z_ground comes from the terrain model or the proposed finished grade,
depending on design stage.

---

## 9. Grading / Surface Constraints

### 9.1 — Target Elevation at Point

**What it does:** Pins a grading point to a known elevation.

**The math:** Z - Z_target = 0. Used for tie-in points where the new grading
must match an existing feature (building pad, road edge, etc.).

---

### 9.2 — Target Slope Between Points

**What it does:** Forces the ground slope between two points to match a
target.

**The math:** The 3D slope between two points is:

    slope = ΔZ / √(ΔE² + ΔN²)

This is rise over horizontal run (not slope distance). The horizontal
distance uses the Euclidean distance in the E-N plane, and the slope is
the ratio of vertical change to this horizontal distance.

---

### 9.3 — Volume Balance (Cut = Fill)

**What it does:** Forces the total earthwork to balance — all cut material
is used as fill, with no import or export.

**The math:** Discretize the grading area into grid cells of area A_cell.
At each cell i, the elevation change is Z_proposed_i - Z_existing_i.
Positive values are fill, negative are cut. The sum:

    Σᵢ (Z_proposed_i - Z_existing_i) · A_cell = 0

means the total volume of fill equals the total volume of cut. This is one
equation with N unknowns (all the grid elevations), so it doesn't fully
determine the solution — it just constrains the average elevation change
to be zero.

**In practice:** Shrink/swell factors (soil expands when excavated, compacts
when placed) modify this equation. Typical factors are 1.2-1.3 for shrinkage.

---

### 9.4 — Maximum Grade

**What it does:** Prevents slopes from exceeding a safe or regulatory maximum.

**The math:** Inequality constraint: |slope| ≤ slope_max. Converted to
equality with a slack variable:

    |slope| - slope_max + s² = 0

The absolute value makes this nonsmooth at zero slope. In practice, you can
reformulate as two one-sided constraints (slope ≤ max and slope ≥ -max),
each with its own slack variable.

---

### 9.5 — Drainage Direction

**What it does:** Ensures water flows in the intended direction.

**The math:** Water flows downhill, so Z_A > Z_B ensures flow from A to B.
The constraint:

    Z_A - Z_B - ε = 0

where ε is a small positive value (minimum head drop, e.g., 0.01 ft) to
prevent a perfectly flat condition where water would pond.

---

## 10. Coordinate Geometry (COGO) Constraints

### 10.1 — Intersection of Two Lines

**What it does:** Finds the point where two lines cross.

**The math:** Each line provides one equation (the point-on-line constraint
from 1.3). Two equations, two unknowns (the intersection point's E and N).
The system is linear, so Newton-Raphson converges in one step — but it's
still useful to include in the general framework for composite problems.

---

### 10.2 — Intersection of Line and Circle

**What it does:** Finds where a line crosses a circle.

**The math:** One point-on-line equation (linear) plus one point-on-circle
equation (quadratic). The system has up to two solutions. The initial guess
determines which one the solver converges to — choose the guess on the
correct side of the circle.

**Geometric meaning:** A line can miss a circle entirely (no solution),
be tangent to it (one solution), or cross it in two places (two solutions).
If Newton-Raphson diverges, it likely means the line misses the circle.

---

### 10.3 — Intersection of Two Circles

**What it does:** Finds where two circles cross.

**The math:** Two point-on-circle equations. Again, up to two solutions
(symmetric about the line connecting the centers). The initial guess
selects which intersection you get.

**Surveyor's use:** This is the geometric basis of distance-distance
intersection — if you know distances from two known points, the unknown
point lies at one of the two intersections of the corresponding circles.

---

### 10.4 — Station/Offset to Easting/Northing

**What it does:** Converts alignment-relative coordinates to absolute
coordinates.

**The math:** Given station S and offset d, the absolute position is:

    (E, N) = alignment_position(S) + d · normal(S)

where alignment_position(S) gives the point on the alignment at that
station, and normal(S) is the perpendicular direction (rotated 90° from
the tangent). The constraint enforces this relationship.

**Why it's nonlinear:** Even though the formula looks simple, both
alignment_position(S) and normal(S) are nonlinear functions of S when the
alignment contains curves and spirals.

---

### 10.5 — Easting/Northing to Station/Offset (Inverse)

**What it does:** Given an absolute point, finds its station and offset
relative to an alignment.

**The math:** This is the inverse of 10.4 and it's harder. The station S
is the value that minimizes the distance from the point P to the alignment.
At the closest point, the vector (P - alignment(S)) must be perpendicular
to the alignment's tangent:

    (P - A(S)) · t(S) = 0

This is the projection equation — it says "the component of the offset
vector along the alignment is zero," meaning you've found the foot of the
perpendicular. The offset d is then the distance:

    d² = |P - A(S)|²

**Complications:** For curved alignments, there may be multiple stations
where the perpendicular condition is satisfied (imagine a point inside a
tight curve — perpendiculars from both "sides" of the curve can reach it).
The initial guess must be close to the correct station.

---

## 11. Compound & Reverse Curve Constraints

### 11.1 — Compound Curve Common Tangent Point

**What it does:** Connects two circular arcs with different radii smoothly.

**The math:** This is G0 + G1 continuity (sections 6.1 and 6.2) applied
at the junction of two arcs. The arcs share a point (positional continuity)
and have the same tangent direction there (tangent continuity). The curvature
is allowed to jump — that's what distinguishes a compound curve from a
spiral transition.

**Design implication:** Compound curves are generally discouraged in modern
highway design because the abrupt curvature change is uncomfortable for
drivers. They're more common in older roads, railways, and site design.

---

### 11.2 — Reverse Curve

**What it does:** Connects two arcs that curve in opposite directions.

**The math:** Same G0 + G1 equations as compound curves, plus a constraint
that the curvatures have opposite signs. If curve 1 curves left (positive κ)
then curve 2 must curve right (negative κ), or vice versa.

The reference document suggests κ₁ + κ₂ = 0 for the equal-radius case
(|κ₁| = |κ₂|). For the general case, the sign check can be:

    κ₁ · κ₂ < 0     (product is negative when signs differ)

This inequality can be enforced outside the solver as a validation, or
converted to an equality if the ratio of radii is known.

**Design implication:** Reverse curves need a tangent section between them
for driver comfort and safety — the superelevation has to transition from
one direction to the other.

---

## 12. Inequality Constraints via Slack Variables

**The problem:** Newton-Raphson solves systems of equalities F(X) = 0. But
many engineering requirements are inequalities: minimum radius, minimum
cover, maximum grade.

**The trick:** Convert g(X) ≥ 0 into an equality by introducing a slack
variable s:

    g(X) - s² = 0

**Why s² and not just s:** If we wrote g(X) - s = 0, the solver could find
s < 0, which would mean g(X) < 0 — violating the original inequality. By
using s², the slack is guaranteed non-negative regardless of the value of s.
The solver can freely choose any real value of s, and s² will always be ≥ 0.

**Jacobian:** The new equation introduces one new unknown (s) and one new
equation. The partial derivative with respect to s is:

    ∂F/∂s = -2s

At the solution, if s = 0, this derivative is zero, which makes the Jacobian
singular. This is the "active constraint" case (the inequality is binding).
Numerically, this can slow convergence. Strategies include:

1. Switching to an active-set method: detect when s is near zero and remove
   the slack variable, converting to a pure equality.
2. Using a small perturbation: g(X) - s² - ε = 0 with a tiny ε.
3. Moving to an interior-point method for problems with many inequalities.

**When to use this vs. a dedicated optimizer:** For a handful of inequality
constraints (2-5), slack variables in Newton-Raphson work fine. For dozens
or hundreds (e.g., grading optimization with slope limits at every grid
cell), switch to a proper constrained optimization algorithm (SQP,
interior-point, augmented Lagrangian).
