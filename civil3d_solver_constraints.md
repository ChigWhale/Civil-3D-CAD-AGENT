# Civil 3D Geometric Solver — Constraint Reference

All constraints are expressed in the form **F(X) = 0** for use in a Newton-Raphson solver.

Notation conventions:
- Points: P = (E, N) for horizontal, (S, Z) for profiles (station, elevation)
- Vectors: **v** = (vₑ, vₙ)
- Subscripts: 1, 2 for endpoints; c for center; t for tangent

---

## 1. Point & Distance Constraints

### 1.1 — Fixed Point
Lock a point to known coordinates.

```
F₁ = E - E_fixed = 0
F₂ = N - N_fixed = 0
```

Jacobian entries: ∂F₁/∂E = 1, ∂F₂/∂N = 1 (all others zero).

### 1.2 — Point-to-Point Distance
Two points must be exactly distance d apart.

```
F = (E₂ - E₁)² + (N₂ - N₁)² - d² = 0
```

Partials:
```
∂F/∂E₁ = -2(E₂ - E₁)     ∂F/∂E₂ =  2(E₂ - E₁)
∂F/∂N₁ = -2(N₂ - N₁)     ∂F/∂N₂ =  2(N₂ - N₁)
```

### 1.3 — Point-on-Line
Point P must lie on the line through A with direction **v** = (vₑ, vₙ).

```
F = (E_P - E_A) · vₙ  -  (N_P - N_A) · vₑ  = 0
```

This is the cross-product (perpendicular distance) set to zero.

### 1.4 — Point-on-Circle
Point P must lie on a circle with center C and radius R.

```
F = (E_P - E_C)² + (N_P - N_C)² - R² = 0
```

### 1.5 — Coincident Points
Two points must be at the same location.

```
F₁ = E₂ - E₁ = 0
F₂ = N₂ - N₁ = 0
```

---

## 2. Angular & Direction Constraints

### 2.1 — Fixed Bearing / Azimuth
A line from P₁ to P₂ must have azimuth θ (measured CW from north).

```
F = atan2(E₂ - E₁, N₂ - N₁) - θ = 0
```

Partials (let dE = E₂-E₁, dN = N₂-N₁, L² = dE²+dN²):
```
∂F/∂E₁ = -dN / L²       ∂F/∂E₂ =  dN / L²
∂F/∂N₁ =  dE / L²       ∂F/∂N₂ = -dE / L²
```

### 2.2 — Angle Between Three Points
Angle at vertex B formed by rays BA and BC must equal α.

```
Let u = A - B, w = C - B
F = atan2(u×w, u·w) - α = 0
```

where `u×w = uₑwₙ - uₙwₑ` and `u·w = uₑwₑ + uₙwₙ`.

### 2.3 — Deflection Angle
At a PI (point of intersection), the deflection angle between the incoming and outgoing tangent must equal Δ.

```
Let d_in  = (E_PI - E_prev, N_PI - N_prev)   [incoming direction]
Let d_out = (E_next - E_PI, N_next - N_PI)    [outgoing direction]

F = atan2(d_in × d_out, d_in · d_out) - Δ = 0
```

### 2.4 — Parallel Lines
Lines P₁P₂ and P₃P₄ must be parallel.

```
F = (E₂ - E₁)(N₄ - N₃) - (N₂ - N₁)(E₄ - E₃) = 0
```

### 2.5 — Perpendicular Lines
Lines P₁P₂ and P₃P₄ must be perpendicular.

```
F = (E₂ - E₁)(E₄ - E₃) + (N₂ - N₁)(N₄ - N₃) = 0
```

---

## 3. Circular Curve Constraints

### 3.1 — Radius
The curve's radius must equal a target value.

```
F = R - R_target = 0
```

### 3.2 — PC on Back Tangent
The PC (point of curvature) must lie on the incoming tangent line through the PI.

```
F = (E_PC - E_PI) · vₙ  -  (N_PC - N_PI) · vₑ  = 0
```

where **v** is the incoming tangent direction vector.

### 3.3 — PT on Ahead Tangent
Same form as 3.2 but for the PT on the outgoing tangent.

```
F = (E_PT - E_PI) · wₙ  -  (N_PT - N_PI) · wₑ  = 0
```

where **w** is the outgoing tangent direction vector.

### 3.4 — Tangent Length (T)
Distance from PI to PC (or PI to PT) must equal `T = R · tan(Δ/2)`.

```
F = sqrt((E_PC - E_PI)² + (N_PC - N_PI)²) - R · tan(Δ/2) = 0
```

### 3.5 — PC Tangency (radius ⊥ tangent)
The radius vector from center C to PC must be perpendicular to the incoming tangent.

```
Let r = PC - C,  t = incoming tangent direction
F = r · t = rₑ·tₑ + rₙ·tₙ = 0
```

### 3.6 — PT Tangency
Same as 3.5 but for the outgoing tangent at PT.

```
Let r = PT - C,  t = outgoing tangent direction
F = r · t = rₑ·tₑ + rₙ·tₙ = 0
```

### 3.7 — Center Equidistant from PC and PT
Ensures the curve center is equidistant from both endpoints.

```
F = (E_PC - E_C)² + (N_PC - N_C)² - (E_PT - E_C)² - (N_PT - N_C)² = 0
```

### 3.8 — Arc Length
Total arc length must equal a target L.

```
F = R · Δ - L = 0        (Δ in radians)
```

### 3.9 — Chord Length
Chord from PC to PT must equal a target value.

```
F = 2R · sin(Δ/2) - C_target = 0
```

### 3.10 — Mid-Ordinate (M)
```
F = R · (1 - cos(Δ/2)) - M_target = 0
```

### 3.11 — External Distance (E)
Distance from PI to the curve midpoint.

```
F = R · (sec(Δ/2) - 1) - E_target = 0
```

---

## 4. Spiral (Clothoid) Constraints

Spirals are defined by `A² = R · L_s` where A is the spiral parameter, R is the circular curve radius, and L_s is the spiral length.

### 4.1 — Spiral Parameter
```
F = A² - R · L_s = 0
```

### 4.2 — Spiral Deflection Angle
The spiral consumes a deflection angle θ_s:

```
F = θ_s - L_s / (2R) = 0
```

### 4.3 — Spiral Tangent Offset (X, Y at end of spiral)
Local coordinates at the end of the spiral (used for TS/SC/CS/ST geometry):

```
X_s = L_s · [1 - θ_s²/10 + θ_s⁴/216 - ...]
Y_s = L_s · [θ_s/3 - θ_s³/42 + θ_s⁵/1320 - ...]
```

Constraint to match computed vs. required:
```
F₁ = X_computed - X_s = 0
F₂ = Y_computed - Y_s = 0
```

### 4.4 — Spiral Tangent Distance (T_s)
Long tangent from TS to PI for a spiral-curve-spiral:

```
p = Y_s - R(1 - cos(θ_s))
k = X_s - R·sin(θ_s)
T_s = k + (R + p) · tan(Δ/2)

F = dist(PI, TS) - T_s = 0
```

### 4.5 — Total Deflection (SCS)
For a spiral–curve–spiral, the total deflection must be consumed:

```
F = Δ - (Δ_c + θ_s_in + θ_s_out) = 0
```

where Δ_c is the circular arc deflection and θ_s are the spiral deflection angles.

---

## 5. Profile / Vertical Curve Constraints

Profiles use (S, Z) — station and elevation.

### 5.1 — Grade (Slope)
Grade between two PVIs must equal target g (as a decimal or %).

```
F = (Z₂ - Z₁) / (S₂ - S₁) - g = 0
```

### 5.2 — Fixed Elevation
```
F = Z - Z_target = 0
```

### 5.3 — Vertical Curve Length
Parabolic vertical curve length must equal L.

```
F = L - L_target = 0
```

### 5.4 — K-Value (Rate of Curvature)
K = L / |g₂ - g₁| where g₁, g₂ are incoming/outgoing grades.

```
F = L / |g₂ - g₁| - K_target = 0
```

### 5.5 — Parabolic Curve Elevation at Station
Elevation at any point along a symmetric parabolic vertical curve:

```
Let x = S - S_BVC
    r = (g₂ - g₁) / L

Z(x) = Z_BVC + g₁·x + (r/2)·x²

F = Z(x) - Z_target = 0
```

### 5.6 — High/Low Point Station
The turning point of a crest or sag curve occurs at:

```
x_turn = -g₁ / r = -g₁·L / (g₂ - g₁)

F = x_turn - x_target = 0
```

### 5.7 — Minimum Cover / Clearance
Elevation at a station must be at least (or at most) a threshold above/below a reference surface.

```
F = Z_profile(S) - Z_surface(S) - cover_min = 0     (equality)
```

For inequality, use a slack variable s ≥ 0:
```
F = Z_profile(S) - Z_surface(S) - cover_min - s² = 0
```

### 5.8 — Sight Distance (Crest)
Stopping sight distance S_sd for a crest curve:

```
F = L - (S_sd² · |g₂ - g₁|) / (200 · (√h₁ + √h₂)²) = 0
```

where h₁ = driver eye height, h₂ = object height.

---

## 6. Tangency & Continuity Constraints

### 6.1 — G0 (Positional) Continuity
Two curve segments share an endpoint.

```
F₁ = E_end_seg1 - E_start_seg2 = 0
F₂ = N_end_seg1 - N_start_seg2 = 0
```

### 6.2 — G1 (Tangent) Continuity
Direction vectors at the junction are parallel and same-sense.

```
Let t₁ = tangent at end of seg1
    t₂ = tangent at start of seg2

F = t₁ × t₂ = t₁ₑ·t₂ₙ - t₁ₙ·t₂ₑ = 0       (cross product = 0)
```

Also verify same sense: `t₁ · t₂ > 0` (checked, not an equation in the system).

### 6.3 — G2 (Curvature) Continuity
Curvatures match at the junction (needed for spiral transitions).

```
F = κ₁_end - κ₂_start = 0

where κ = 1/R for circular arcs
      κ = L/(A²) for spirals at distance L from start
      κ = 0 for tangent lines
```

---

## 7. Offset & Corridor Constraints

### 7.1 — Horizontal Offset
Point P must be at perpendicular offset d from a reference alignment at station S.

```
Let t = unit tangent of alignment at S
    n = (-tₙ, tₑ)  [left-hand normal]
    A = point on alignment at station S

F₁ = E_P - (E_A + d · nₑ) = 0
F₂ = N_P - (N_A + d · nₙ) = 0
```

### 7.2 — Cross Slope
Between two points at offsets d₁ and d₂ from centerline, the cross slope must equal m.

```
F = (Z₂ - Z₁) / (d₂ - d₁) - m = 0
```

### 7.3 — Lane Width
Distance between two offset lines must equal w.

```
F = |d_right - d_left| - w = 0
```

### 7.4 — Superelevation
At station S, the cross slope must match the superelevation schedule value e(S).

```
F = (Z_edge - Z_CL) / offset - e(S) = 0
```

### 7.5 — Daylight (Cut/Fill Slope Intercept)
A grading slope from the edge of pavement must intercept the existing ground surface. If the slope is m:1 (horizontal:vertical):

```
Let Z_edge = edge elevation, Z_ground(d) = ground elevation at offset d
    slope = 1/m (rise/run)

F = Z_edge + (d - d_edge) · slope - Z_ground(d) = 0
```

This requires iterating d as an unknown.

---

## 8. Pipe Network Constraints

### 8.1 — Invert Elevation Match
Pipe invert at a structure must match the structure's invert elevation.

```
F = Z_pipe_invert - Z_structure_invert = 0
```

### 8.2 — Minimum Pipe Slope
Pipe slope between two structures must meet minimum.

```
F = (Z_inv_US - Z_inv_DS) / L_pipe - slope_min = 0
```

For inequality: use slack variable.

### 8.3 — Minimum Cover
Pipe crown must be at least cover_min below ground.

```
Z_crown = Z_invert + D_pipe (pipe diameter)
F = Z_ground - Z_crown - cover_min = 0
```

### 8.4 — Hydraulic Capacity (Manning's)
Flow capacity Q for gravity pipe:

```
Q = (1/n) · A · R^(2/3) · S^(1/2)

F = Q_computed - Q_required = 0
```

where n = roughness, A = flow area, R = hydraulic radius, S = pipe slope.

### 8.5 — Structure Rim Elevation
```
F = Z_rim - Z_ground_at_structure = 0
```

---

## 9. Grading / Surface Constraints

### 9.1 — Target Elevation at Point
```
F = Z - Z_target = 0
```

### 9.2 — Target Slope Between Points
```
F = (Z₂ - Z₁) / sqrt((E₂-E₁)² + (N₂-N₁)²) - slope_target = 0
```

### 9.3 — Volume Balance (Cut = Fill)
Over a grading region with N grid cells of area A_cell:

```
F = Σᵢ (Z_proposed_i - Z_existing_i) · A_cell = 0
```

### 9.4 — Maximum Grade
Surface slope must not exceed a maximum. Use slack variable:

```
F = |(Z₂ - Z₁) / L₁₂| - slope_max + s² = 0    (s is slack)
```

### 9.5 — Drainage Direction
Water must flow from point A to point B (Z_A > Z_B):

```
F = Z_A - Z_B - ε = 0     (ε is a small positive minimum drop)
```

---

## 10. Coordinate Geometry (COGO) Constraints

### 10.1 — Intersection of Two Lines
Lines P₁→P₂ and P₃→P₄ must intersect at point Q.

```
F₁: Q lies on line P₁P₂   →  (E_Q - E₁)(N₂-N₁) - (N_Q - N₁)(E₂-E₁) = 0
F₂: Q lies on line P₃P₄   →  (E_Q - E₃)(N₄-N₃) - (N_Q - N₃)(E₄-E₃) = 0
```

### 10.2 — Intersection of Line and Circle
Point Q lies on both a line and a circle.

```
F₁ = (E_Q - E₁)(N₂-N₁) - (N_Q - N₁)(E₂-E₁) = 0          [on line]
F₂ = (E_Q - E_C)² + (N_Q - N_C)² - R² = 0                  [on circle]
```

Note: produces two solutions — initial guess selects which.

### 10.3 — Intersection of Two Circles
```
F₁ = (E_Q - E_C1)² + (N_Q - N_C1)² - R₁² = 0
F₂ = (E_Q - E_C2)² + (N_Q - N_C2)² - R₂² = 0
```

### 10.4 — Station/Offset to Easting/Northing
Given station S and offset d from an alignment, compute (E, N):

```
F₁ = E - E_align(S) - d · nₑ(S) = 0
F₂ = N - N_align(S) - d · nₙ(S) = 0
```

where n(S) is the left-hand normal at station S.

### 10.5 — Easting/Northing to Station/Offset (Inverse)
Given a point P, find station S and offset d. This requires minimizing distance to the alignment, yielding:

```
F₁ = (E_P - E_align(S)) · tₑ(S) + (N_P - N_align(S)) · tₙ(S) = 0    [projection onto tangent = 0]
F₂ = (E_P - E_align(S))² + (N_P - N_align(S))² - d² = 0               [offset distance]
```

---

## 11. Compound & Reverse Curve Constraints

### 11.1 — Compound Curve Common Tangent Point
Two curves share a point where they are tangent (G1 continuous) but have different radii.

```
F₁ = E_end_curve1 - E_start_curve2 = 0          [positional]
F₂ = N_end_curve1 - N_start_curve2 = 0
F₃ = t₁ × t₂ = 0                                 [tangent continuity]
```

### 11.2 — Reverse Curve
Two curves with opposite curvature directions sharing a tangent point. Same equations as 11.1 but the centers must be on opposite sides:

```
F₄ = sign(κ₁) + sign(κ₂) = 0     (curvatures have opposite signs)
```

Implemented as: `F₄ = κ₁ + κ₂ = 0` when |κ₁| = |κ₂|, or more generally check that centers are on opposite sides of the tangent line.

---

## 12. Inequality Constraints via Slack Variables

Newton-Raphson solves equalities. Convert inequalities `g(X) ≥ 0` to equalities by introducing a slack variable s:

```
g(X) - s² = 0
```

s is added to the state vector X. The squaring ensures the slack is non-negative without explicit bounds.

Common inequalities in Civil 3D:
- Minimum radius: `R - R_min - s² = 0`
- Minimum grade: `|g| - g_min - s² = 0`
- Minimum cover: as shown in 5.7 and 8.3
- Maximum superelevation: `e_max - |e| - s² = 0`
- Minimum K-value: `K - K_min - s² = 0`

---

## Summary: Constraint Count Checklist

For a well-posed system, **# equations = # unknowns**.

| Object | Typical Unknowns per Element | Typical Constraints per Element |
|--------|------|------|
| Point | 2 (E, N) | 2 (fixed, or on-line + distance) |
| Line segment | 4 (two endpoints) | 4 (bearings, distances, connections) |
| Circular curve | 5–7 (C, R, PC, PT) | 5–7 (tangency, radius, deflection) |
| Spiral | 3–4 (L_s, A, θ_s, attach point) | 3–4 (parameter, G2 continuity) |
| Profile PVI | 1–2 (Z, possibly S) | 1–2 (grade, elevation) |
| Vertical curve | 1–2 (L or K) | 1–2 (K-value, sight distance) |
| Pipe segment | 2–3 (inverts, slope) | 2–3 (cover, slope, capacity) |
| Grading point | 1 (Z) | 1 (slope, target elev, volume) |
