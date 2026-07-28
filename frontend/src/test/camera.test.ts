/**
 * Frame analysis and the flame-pixel predicate.
 *
 * This is the highest-leverage code in the camera path: everything the server
 * decides rests on `flame_ratio`, and `flame_ratio` rests entirely on
 * `isFlamePixel`. Tested directly against colours rather than only through a
 * synthetic canvas, so a regression names the exact hue that broke.
 */

import { describe, expect, it } from 'vitest';

import {
  SAMPLE_HEIGHT,
  SAMPLE_WIDTH,
  analyseFrame,
  isFlamePixel,
} from '@/lib/cameraSensor';

/** Build an RGBA buffer where every pixel is the same colour. */
function solid(r: number, g: number, b: number, width = 8, height = 8) {
  const data = new Uint8ClampedArray(width * height * 4);
  for (let i = 0; i < width * height; i += 1) {
    data[i * 4] = r;
    data[i * 4 + 1] = g;
    data[i * 4 + 2] = b;
    data[i * 4 + 3] = 255;
  }
  return { data, width, height };
}

/** Vertical stripes, so neighbouring pixels differ — a maximally sharp scene. */
function stripes(width = 8, height = 8) {
  const data = new Uint8ClampedArray(width * height * 4);
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const index = (y * width + x) * 4;
      const value = x % 2 === 0 ? 0 : 255;
      data[index] = value;
      data[index + 1] = value;
      data[index + 2] = value;
      data[index + 3] = 255;
    }
  }
  return { data, width, height };
}

describe('isFlamePixel', () => {
  it('accepts the colours a flame actually produces', () => {
    expect(isFlamePixel(255, 160, 40)).toBe(true); // orange flame body
    expect(isFlamePixel(255, 220, 150)).toBe(true); // hot near-white core
    expect(isFlamePixel(230, 120, 30)).toBe(true); // deeper orange edge
  });

  it('rejects a saturated red object', () => {
    // Pure red has no green: fire is graded through orange, a red mug is not.
    expect(isFlamePixel(255, 0, 0)).toBe(false);
    expect(isFlamePixel(200, 10, 10)).toBe(false);
  });

  it('rejects cool and neutral colours', () => {
    expect(isFlamePixel(0, 0, 0)).toBe(false); // black
    expect(isFlamePixel(255, 255, 255)).toBe(false); // white: no red dominance
    expect(isFlamePixel(40, 90, 220)).toBe(false); // blue
    expect(isFlamePixel(60, 180, 90)).toBe(false); // green
  });

  it('rejects dark reds that are merely shadow', () => {
    expect(isFlamePixel(90, 50, 20)).toBe(false);
  });

  it('rejects magenta, where blue outruns green', () => {
    // A screen or a neon sign. Red-dominant, but not warm.
    expect(isFlamePixel(240, 90, 200)).toBe(false);
  });
});

describe('analyseFrame', () => {
  it('reports no flame for a black frame', () => {
    const { data, width, height } = solid(0, 0, 0);
    const metrics = analyseFrame(data, width, height);
    expect(metrics.flame_ratio).toBe(0);
    expect(metrics.luminance).toBe(0);
  });

  it('reports a full frame of flame as ratio 1', () => {
    const { data, width, height } = solid(255, 160, 40);
    expect(analyseFrame(data, width, height).flame_ratio).toBe(1);
  });

  it('scales the ratio with the flame-coloured area', () => {
    const width = 10;
    const height = 10;
    const data = new Uint8ClampedArray(width * height * 4);
    for (let i = 0; i < width * height; i += 1) {
      const flame = i < 25; // a quarter of the frame
      data[i * 4] = flame ? 255 : 20;
      data[i * 4 + 1] = flame ? 160 : 20;
      data[i * 4 + 2] = flame ? 40 : 20;
      data[i * 4 + 3] = 255;
    }
    expect(analyseFrame(data, width, height).flame_ratio).toBeCloseTo(0.25, 5);
  });

  it('measures luminance perceptually, not as a plain mean', () => {
    // Green looks far brighter than blue at the same numeric value; a naive
    // average would call these identical.
    const g = solid(0, 255, 0);
    const b = solid(0, 0, 255);
    const green = analyseFrame(g.data, g.width, g.height);
    const blue = analyseFrame(b.data, b.width, b.height);
    expect(green.luminance).toBeGreaterThan(blue.luminance);
  });

  it('reports a sharp scene as low haze', () => {
    const { data, width, height } = stripes();
    expect(analyseFrame(data, width, height).haze_index).toBe(0);
  });

  it('reports a featureless scene as maximum haze', () => {
    // Every pixel identical: no detail at all, exactly what heavy smoke does.
    const { data, width, height } = solid(120, 120, 120);
    expect(analyseFrame(data, width, height).haze_index).toBe(1);
  });

  it('keeps every metric inside the range the API accepts', () => {
    const cases = [
      solid(0, 0, 0),
      solid(255, 255, 255),
      solid(255, 160, 40),
      stripes(),
    ];
    for (const { data, width, height } of cases) {
      const metrics = analyseFrame(data, width, height);
      for (const value of Object.values(metrics)) {
        expect(value).toBeGreaterThanOrEqual(0);
        expect(value).toBeLessThanOrEqual(1);
      }
    }
  });

  it('handles a zero-sized frame without dividing by zero', () => {
    const metrics = analyseFrame(new Uint8ClampedArray(0), 0, 0);
    expect(metrics.flame_ratio).toBe(0);
    expect(metrics.luminance).toBe(0);
    expect(Number.isFinite(metrics.haze_index)).toBe(true);
  });

  it('analyses at a fixed small resolution', () => {
    // The constants are part of the contract: raising them silently would
    // multiply per-frame cost on a phone.
    expect(SAMPLE_WIDTH * SAMPLE_HEIGHT).toBeLessThanOrEqual(32_000);
  });
});
