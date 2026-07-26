# FireProtect — Generated Assets

Every file here is **generated procedurally** by
`frontend/scripts/generate_assets.py`. There is no stock photography, no
CDN link, no placeholder service and no external URL anywhere in the
build. Regenerate with:

```bash
python frontend/scripts/generate_assets.py
```

## Art direction

Derived from `design.md`, which specifies a black-and-white palette with no
brand accent colour and states that photography supplies every other hue.
These images are that photographic layer:

- **Single lighting model** — one soft source, upper-centre, falling to pure
  black at the frame edges.
- **Luminance-first** — warm hue appears only in the thermal/ember pieces and
  is desaturated (warmth 0.62–0.70) so it never reads as a brand accent.
- **Domain-bound** — thermal plumes, IoT mesh topology, PCB routing, ember
  particles. Nothing that could belong to a generic dashboard.

## Contrast method

`design.md` forbids scrims and gradient overlays on dark canvas ("Grade the
photo, not the canvas"), so compliance is achieved by **darkening the image
itself**, not by layering anything over it.

Each background is divided into a 12×12 grid and the *worst* cell is
measured — averaging across the whole frame would hide a bright hotspot that
text might land on. Images are graded 14% darker per pass until white body
text clears 4.5:1 against every cell.

## Files

| File | Purpose | Used in | Dimensions | Size | Contrast check |
|---|---|---|---|---|---|
| `backdrop-circuit-trace-desktop.webp` | Threshold config section backdrop | Threshold configuration panel | 2560x1440 | 48 KB | PASS - white on brightest region = 20.62:1 (AA body needs 4.5:1) |
| `backdrop-circuit-trace-mobile.webp` | Threshold config backdrop, mobile crop | Threshold configuration panel below 768px | 1280x720 | 33 KB | PASS - white on brightest region = 20.25:1 (AA body needs 4.5:1) |
| `backdrop-sensor-mesh-desktop.webp` | Section backdrop | Device grid section | 2560x1440 | 16 KB | PASS - white on brightest region = 20.87:1 (AA body needs 4.5:1) |
| `backdrop-sensor-mesh-mobile.webp` | Section backdrop, mobile crop | Device grid section below 768px | 1280x720 | 7 KB | PASS - white on brightest region = 20.50:1 (AA body needs 4.5:1) |
| `empty-state-no-alerts.webp` | Empty state illustration | Alert history when no alerts exist | 1200x800 | 4 KB | PASS - white on brightest region = 19.43:1 (AA body needs 4.5:1) |
| `empty-state-no-devices.webp` | Empty state illustration | Device list when no devices have reported | 1200x800 | 11 KB | PASS - white on brightest region = 19.44:1 (AA body needs 4.5:1) |
| `favicon.svg` | Favicon / app icon | index.html | vector | 426 B | N/A - vector, inherits currentColor from the token palette |
| `hero-thermal-plume-desktop.webp` | Primary hero backdrop | Dashboard hero band (`AppShell`) | 2560x1440 | 15 KB | PASS - white on brightest region = 5.47:1 (AA body needs 4.5:1); graded 1 pass(es) darker to comply |
| `hero-thermal-plume-mobile.webp` | Hero backdrop, mobile art-direction crop | Dashboard hero band below 768px | 1280x720 | 4 KB | PASS - white on brightest region = 5.64:1 (AA body needs 4.5:1); graded 2 pass(es) darker to comply |
| `icon-air-quality.svg` | UI icon | Sensor cards, device status, alerts | vector | 375 B | N/A - vector, inherits currentColor from the token palette |
| `icon-alert.svg` | UI icon | Sensor cards, device status, alerts | vector | 324 B | N/A - vector, inherits currentColor from the token palette |
| `icon-device.svg` | UI icon | Sensor cards, device status, alerts | vector | 372 B | N/A - vector, inherits currentColor from the token palette |
| `icon-flame.svg` | UI icon | Sensor cards, device status, alerts | vector | 371 B | N/A - vector, inherits currentColor from the token palette |
| `icon-humidity.svg` | UI icon | Sensor cards, device status, alerts | vector | 301 B | N/A - vector, inherits currentColor from the token palette |
| `icon-shield.svg` | UI icon | Sensor cards, device status, alerts | vector | 360 B | N/A - vector, inherits currentColor from the token palette |
| `icon-smoke.svg` | UI icon | Sensor cards, device status, alerts | vector | 350 B | N/A - vector, inherits currentColor from the token palette |
| `icon-temperature.svg` | UI icon | Sensor cards, device status, alerts | vector | 367 B | N/A - vector, inherits currentColor from the token palette |
| `logo-fireprotect.svg` | Wordmark | Top navigation bar | vector | 673 B | N/A - vector, inherits currentColor from the token palette |
| `texture-ember-drift-desktop.webp` | Alert section backdrop | Alert history section | 2560x1440 | 49 KB | PASS - white on brightest region = 13.23:1 (AA body needs 4.5:1) |
| `texture-ember-drift-mobile.webp` | Alert section backdrop, mobile crop | Alert history section below 768px | 1280x720 | 37 KB | PASS - white on brightest region = 11.70:1 (AA body needs 4.5:1) |

## Generation notes

### `backdrop-circuit-trace-desktop.webp`

PCB routing on a 48px grid using Manhattan and 45-degree runs only, the way real boards are routed, terminating in via pads. The dimmest asset in the set (trace values 20-62) because it sits behind form controls.

Fallbacks: backdrop-circuit-trace-desktop.jpg (155 KB fallback)

### `backdrop-circuit-trace-mobile.webp`

Circuit motif regenerated at mobile resolution.

Fallbacks: backdrop-circuit-trace-mobile.jpg (87 KB fallback)

### `backdrop-sensor-mesh-desktop.webp`

An IoT mesh: 77 jittered nodes linked only to near neighbours (a real mesh is local, not fully connected), supersampled 2x and downscaled for clean edges. Node brightness varies with a per-node weight to suggest signal strength.

Fallbacks: backdrop-sensor-mesh-desktop.jpg (51 KB fallback)

### `backdrop-sensor-mesh-mobile.webp`

Mesh motif regenerated at mobile resolution with the same seed family.

Fallbacks: backdrop-sensor-mesh-mobile.jpg (22 KB fallback)

### `empty-state-no-alerts.webp`

A flat, quiet baseline trace inside a hairline ring - the visual of nothing happening. At 2x the 600x400 display size.

Fallbacks: empty-state-no-alerts.jpg (12 KB fallback)

### `empty-state-no-devices.webp`

An unpopulated mesh: seven ring-outlined nodes with hairline links and nothing lit, at 2x the 600x400 display size. Deliberately the visual inverse of the populated sensor-mesh backdrop.

Fallbacks: empty-state-no-devices.jpg (23 KB fallback)

### `favicon.svg`

The logo mark alone on a solid canvas-night square, sized for 32x32.

### `hero-thermal-plume-desktop.webp`

A convective heat plume rising through cold air, built from nine stacked radial sources drifting upward with quadratic falloff, broken up by value-noise turbulence so it reads as a captured thermal frame rather than a smooth airbrush gradient. Ember ramp desaturated to warmth 0.62.

Fallbacks: hero-thermal-plume-desktop.jpg (42 KB fallback)

### `hero-thermal-plume-mobile.webp`

The same plume regenerated at 1280x720 rather than downscaled, so the focal subject stays centred at mobile widths per design.md's art-direction crop rule.

Fallbacks: hero-thermal-plume-mobile.jpg (12 KB fallback)

### `icon-air-quality.svg`

A 24x24 stroke icon (air-quality) drawn on a 1.5px stroke grid with round caps and joins, inheriting `currentColor` so it always matches the surrounding token colour.

### `icon-alert.svg`

A 24x24 stroke icon (alert) drawn on a 1.5px stroke grid with round caps and joins, inheriting `currentColor` so it always matches the surrounding token colour.

### `icon-device.svg`

A 24x24 stroke icon (device) drawn on a 1.5px stroke grid with round caps and joins, inheriting `currentColor` so it always matches the surrounding token colour.

### `icon-flame.svg`

A 24x24 stroke icon (flame) drawn on a 1.5px stroke grid with round caps and joins, inheriting `currentColor` so it always matches the surrounding token colour.

### `icon-humidity.svg`

A 24x24 stroke icon (humidity) drawn on a 1.5px stroke grid with round caps and joins, inheriting `currentColor` so it always matches the surrounding token colour.

### `icon-shield.svg`

A 24x24 stroke icon (shield) drawn on a 1.5px stroke grid with round caps and joins, inheriting `currentColor` so it always matches the surrounding token colour.

### `icon-smoke.svg`

A 24x24 stroke icon (smoke) drawn on a 1.5px stroke grid with round caps and joins, inheriting `currentColor` so it always matches the surrounding token colour.

### `icon-temperature.svg`

A 24x24 stroke icon (temperature) drawn on a 1.5px stroke grid with round caps and joins, inheriting `currentColor` so it always matches the surrounding token colour.

### `logo-fireprotect.svg`

A flame silhouette inscribed in a sensor aperture ring, set against FIREPROTECT in the display face at 1.6px tracking - matching design.md's display token. Pure white on transparent, per the overlay nav treatment.

### `texture-ember-drift-desktop.webp`

1,400 ember particles distributed with a 1.8-power bias toward the lower frame so density thins as they rise, blended with a low glow from below. Warmth 0.70 - the warmest asset in the set, and used only behind the alert timeline where the association is meaningful.

Fallbacks: texture-ember-drift-desktop.jpg (99 KB fallback)

### `texture-ember-drift-mobile.webp`

Ember texture regenerated at mobile resolution.

Fallbacks: texture-ember-drift-mobile.jpg (64 KB fallback)

## Budget

- Files: **20**
- Total (primary formats): **232 KB**
- Per-background ceiling: 400 KB (enforced in `export()`)
- Only the hero background loads eagerly; everything else is lazy-loaded,
  keeping first-load page weight under the 2 MB budget.
