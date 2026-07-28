/**
 * Turn a video frame into fire evidence, in the browser.
 *
 * ## Why this file exists
 *
 * A phone has no gas sensor and no thermometer for the room it is sitting in.
 * There is no web API that returns smoke ppm, CO2, or ambient temperature —
 * those numbers only exist if somebody wired up an MQ-2 and a DHT22. What a
 * browser does have is a camera, and fire is one of the few hazards that is
 * genuinely visible.
 *
 * So the camera is treated as its own sensor with its own channels, not as a
 * stand-in for hardware. Nothing here is converted into a fake ppm figure.
 *
 * ## Privacy
 *
 * No image data ever leaves the device. Frames are drawn to an offscreen
 * canvas, reduced to three numbers, and discarded. What reaches the network is
 * `{flame_ratio, luminance, haze_index}` — three floats that cannot be
 * reconstructed into a picture of anyone's living room. This is a design
 * requirement, not a bandwidth optimisation.
 *
 * ## What is measured
 *
 * **flame_ratio** — fraction of pixels that look like fire. Fire has a very
 * particular signature: bright, red-dominant, and ordered R ≥ G ≥ B. That
 * ordering is what separates flame from a red object; a red mug is red but not
 * *graded* through orange into white at its core. The test below combines the
 * RGB rule from Chen et al. with a YCbCr chrominance rule (Cr > Cb), which is
 * far more robust to a camera's automatic white balance shifting the absolute
 * red level.
 *
 * **luminance** — mean perceptual brightness. Context only; the server never
 * gates on it, because a fire at night is still a fire.
 *
 * **haze_index** — loss of high-frequency detail. Smoke scatters light and
 * flattens local contrast, so a room filling with smoke goes soft before it
 * goes dark. Measured as the mean absolute difference between neighbouring
 * pixels, inverted. This also reads high for a smudged or out-of-focus lens,
 * which is exactly why the *server* scores it against a per-device baseline
 * rather than an absolute threshold.
 *
 * Flicker — the strongest signal of all — is deliberately NOT computed here.
 * It is derived server-side from the history of `flame_ratio`, so a client
 * cannot claim a convincing flicker score for a static image.
 *
 * ## Cost
 *
 * The frame is downscaled to 160×120 (19,200 pixels) before analysis. At 2 Hz
 * that is ~40k pixel reads per second, which is nothing — but a full-resolution
 * frame would be 100× that and would visibly heat a phone.
 */

import type { CameraFrameMetrics } from '@/types';

/** Analysis resolution. Small enough to be free, large enough to see a candle. */
export const SAMPLE_WIDTH = 160;
export const SAMPLE_HEIGHT = 120;

/**
 * Minimum red channel for a pixel to be considered flame.
 * Below this the pixel is too dark to be burning, whatever its hue.
 */
const RED_MIN = 115;

/** Red must lead green by this much — the "graded through orange" property. */
const RED_OVER_GREEN = 18;

/** Green must lead blue. Blue-tinted "red" is a screen or a fabric, not fire. */
const GREEN_OVER_BLUE = 8;

/** Overall brightness floor, so deep-red shadows do not count. */
const INTENSITY_MIN = 95;

/**
 * Neighbour-difference value treated as "fully sharp".
 * Chosen from typical indoor scenes: a normal room sits around 12–25, a
 * smoke-filled or heavily blurred one drops below 5.
 */
const SHARPNESS_FULL = 22;

export interface CameraAnalysisFrame extends CameraFrameMetrics {
  /** Wall-clock time of the sample, for the local sparkline. */
  at: number;
}

/**
 * Classify one pixel as flame-like.
 *
 * Exported for testing: this predicate is the single most consequential
 * decision in the whole camera path, and it deserves direct test cases rather
 * than only being exercised through a synthetic canvas.
 */
export function isFlamePixel(r: number, g: number, b: number): boolean {
  if (r < RED_MIN) return false;
  if (r < g + RED_OVER_GREEN) return false;
  if (g < b + GREEN_OVER_BLUE) return false;

  const intensity = (r + g + b) / 3;
  if (intensity < INTENSITY_MIN) return false;

  // YCbCr chrominance test. Cr (red-difference) exceeding Cb (blue-difference)
  // is a stable property of flame across exposure and white-balance changes,
  // where a raw RGB threshold alone drifts as the camera auto-adjusts.
  const cb = 128 - 0.168736 * r - 0.331264 * g + 0.5 * b;
  const cr = 128 + 0.5 * r - 0.418688 * g - 0.081312 * b;
  return cr > cb;
}

/**
 * Reduce one RGBA frame to the three metrics the server scores.
 *
 * Takes raw pixel data rather than a canvas so it can be tested without a DOM.
 */
export function analyseFrame(
  data: Uint8ClampedArray,
  width: number,
  height: number,
): CameraFrameMetrics {
  let flamePixels = 0;
  let luminanceSum = 0;
  let gradientSum = 0;
  let gradientSamples = 0;

  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const index = (y * width + x) * 4;
      // Non-null assertions: the loop bounds guarantee these are in range, and
      // `noUncheckedIndexedAccess` cannot see that. A `?? 0` here would be
      // dead code executed 19,200 times per frame.
      const r = data[index]!;
      const g = data[index + 1]!;
      const b = data[index + 2]!;

      if (isFlamePixel(r, g, b)) flamePixels += 1;

      // Rec. 601 luma — perceptual weighting, so green counts far more than
      // blue, matching how bright the scene actually looks.
      const luma = 0.299 * r + 0.587 * g + 0.114 * b;
      luminanceSum += luma;

      // Horizontal neighbour difference as a cheap sharpness proxy. One
      // direction is enough: smoke degrades detail isotropically, and
      // sampling both axes would double the cost for no extra signal.
      if (x + 1 < width) {
        const next = index + 4;
        const nextLuma =
          0.299 * data[next]! + 0.587 * data[next + 1]! + 0.114 * data[next + 2]!;
        gradientSum += Math.abs(nextLuma - luma);
        gradientSamples += 1;
      }
    }
  }

  const pixels = width * height;
  const meanGradient = gradientSamples > 0 ? gradientSum / gradientSamples : 0;
  const sharpness = Math.min(1, meanGradient / SHARPNESS_FULL);

  return {
    flame_ratio: pixels > 0 ? flamePixels / pixels : 0,
    luminance: pixels > 0 ? luminanceSum / pixels / 255 : 0,
    // Inverted sharpness: 0 = crisp scene, 1 = completely featureless.
    haze_index: 1 - sharpness,
  };
}

/** Errors this module raises, mapped to something a person can act on. */
export function describeCameraError(error: unknown): string {
  const name = (error as { name?: string } | null)?.name ?? '';
  switch (name) {
    case 'NotAllowedError':
    case 'PermissionDeniedError':
      return 'Camera access was blocked. Allow it in your browser’s site settings, then try again.';
    case 'NotFoundError':
    case 'DevicesNotFoundError':
      return 'No camera was found on this device.';
    case 'NotReadableError':
    case 'TrackStartError':
      return 'The camera is already in use by another app. Close it and try again.';
    case 'OverconstrainedError':
      return 'This camera cannot provide a usable video stream.';
    case 'SecurityError':
      return 'Camera access needs a secure connection (https).';
    default:
      break;
  }
  if (!window.isSecureContext) {
    return 'Camera access needs a secure connection (https). Open the site over https and try again.';
  }
  if (typeof navigator.mediaDevices?.getUserMedia !== 'function') {
    return 'This browser does not support camera capture.';
  }
  return error instanceof Error ? error.message : 'Could not start the camera.';
}

/**
 * Owns the camera stream and the analysis canvas.
 *
 * A class rather than a hook because the lifecycle is genuinely imperative —
 * a MediaStream must be stopped exactly once, and letting React's effect
 * scheduling own that leads to tracks left running after unmount, which shows
 * up to the user as a camera light that never goes off.
 */
export class CameraSensor {
  private stream: MediaStream | null = null;
  private canvas: HTMLCanvasElement | null = null;
  private context: CanvasRenderingContext2D | null = null;

  get active(): boolean {
    return this.stream !== null;
  }

  /** Request the camera and attach it to a <video>. Rejects if denied. */
  async start(video: HTMLVideoElement): Promise<void> {
    if (!window.isSecureContext) {
      throw new Error(
        'Camera access needs a secure connection (https). Open the site over https and try again.',
      );
    }
    if (typeof navigator.mediaDevices?.getUserMedia !== 'function') {
      throw new Error('This browser does not support camera capture.');
    }

    this.stream = await navigator.mediaDevices.getUserMedia({
      // Rear camera where there is one: you point a phone at the hazard, not
      // at your own face. `ideal` rather than `exact` so a laptop with only a
      // front camera still works instead of throwing OverconstrainedError.
      video: {
        facingMode: { ideal: 'environment' },
        width: { ideal: 640 },
        height: { ideal: 480 },
      },
      audio: false,
    });

    video.srcObject = this.stream;
    video.setAttribute('playsinline', 'true');
    video.muted = true;
    await video.play();

    this.canvas = document.createElement('canvas');
    this.canvas.width = SAMPLE_WIDTH;
    this.canvas.height = SAMPLE_HEIGHT;
    // willReadFrequently: without it browsers keep the canvas on the GPU and
    // every getImageData stalls on a readback, which at 2 Hz is a visible
    // stutter on a phone.
    this.context = this.canvas.getContext('2d', { willReadFrequently: true });
    if (this.context === null) {
      this.stop();
      throw new Error('Could not create a canvas to analyse the video.');
    }
  }

  /** Analyse the current frame, or null if the video has no data yet. */
  sample(video: HTMLVideoElement): CameraAnalysisFrame | null {
    if (this.context === null || this.canvas === null) return null;
    if (video.videoWidth === 0 || video.videoHeight === 0) return null;

    this.context.drawImage(video, 0, 0, SAMPLE_WIDTH, SAMPLE_HEIGHT);
    let image: ImageData;
    try {
      image = this.context.getImageData(0, 0, SAMPLE_WIDTH, SAMPLE_HEIGHT);
    } catch {
      // Tainted canvas. Cannot happen with a same-origin MediaStream, but a
      // thrown SecurityError here would otherwise kill the capture loop.
      return null;
    }

    return {
      ...analyseFrame(image.data, SAMPLE_WIDTH, SAMPLE_HEIGHT),
      at: Date.now(),
    };
  }

  /** Release the camera. Safe to call repeatedly. */
  stop(): void {
    this.stream?.getTracks().forEach((track) => {
      track.stop();
    });
    this.stream = null;
    this.canvas = null;
    this.context = null;
  }
}
