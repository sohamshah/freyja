import { useEffect, useRef, useState } from 'react'
import { AnimatedTopographicMark } from './AnimatedTopographicMark'

/**
 * Boot splash — a WebGL pearl-membrane animation that emits a reveal
 * ripple to dissolve itself into the app.
 *
 * Choreography:
 *   t=0          shader begins; icon hidden
 *   t=2.5s       first wave decay phase begins, icon starts fading in
 *   t=8s         icon at full opacity, pool still settling
 *   t=10s        REVEAL — a second wave emanates from the icon, its
 *                expanding radius drives an alpha mask in the shader.
 *                The splash dissolves from the icon outward as the wave
 *                passes; the dissolution boundary glows with pearl
 *                iridescence from the wave's own normal field.
 *   t=13s        reveal complete, canvas fully transparent → splash
 *                unmounts, app underneath is fully visible.
 *
 * Both the icon and the welcome icon underneath are pinned to viewport
 * centre via fixed positioning, so they overlap pixel-exact throughout
 * the splash. No migration logic — the icon never moves. When the
 * shader's alpha hits zero at the icon's position the welcome icon
 * shows through with perfect continuity.
 *
 * Visual vocabulary (verbatim from the "pearl boot" reference):
 *   - Soft studio light field (cool white / rose-water / ice blue)
 *   - Damped-sinc aqueous membrane with chromatic refraction
 *   - Mother-of-pearl iridescence on the wave crests
 *   - Broad silky studio softbox specular
 *
 * Smooth start: 800ms canvas opacity fade-in + smoothstep on-ramp over
 * the first 12% of the splash-progress curve so the wave's birth
 * doesn't snap out.
 */

const RIPPLE_DURATION_MS = 14_000
// Timing beats.
//   t=0-3s        first ripple at peak amplitude, icon hidden
//   t=3-4s        icon fades in (smoothstep over 1s)
//   t=4-7.5s      gravity well builds (3.5s easeInQuad)
//                   - all of the icon's rings compress toward centre,
//                     converging on a near-singular point
//                   - pearl substrate develops a deep Gaussian
//                     depression at the icon's location
//   t=7.5s        SNAP: gravity peaks AT THE SAME INSTANT the cascade fires
//   t=7.5-7.8s    overlapping release window — gp rebounds 1→0,
//                 icon fades out, snap burst peaks, W0 begins expansion
//   t=7.5-12.5s   reveal cascade dissolves the splash
const ICON_FADE_IN_START_MS = 3_000
const ICON_FADE_IN_END_MS = 4_000
const GRAVITY_BUILD_START_MS = 4_000
const GRAVITY_PEAK_MS = 7_500   // also REVEAL_START_MS — single moment of release
const REVEAL_START_MS = 7_500
const RELEASE_END_MS = 7_800    // gp finishes rebound, icon fully faded
const REVEAL_DURATION_MS = 5_000
const SKIP_GUARD_MS = 600

// Splash icon SVG size — matches the hero-welcome mark exactly so the
// crossfade into the welcome layout is geometry-exact.
const ICON_SIZE = 190

const VERTEX_SRC = `
attribute vec4 a_position;
void main() { gl_Position = a_position; }
`

const FRAGMENT_SRC = `
precision highp float;

uniform vec2 u_resolution;
uniform float u_time;
uniform float u_splash_progress;
uniform float u_reveal_progress;
uniform float u_gravity_pull;

// Soft studio light field — diffuse pearl tones drifting slowly.
// Sampled by the refraction path so the wave reads as thick pearl
// rather than cheap glass.
vec3 getStudioBackground(vec2 uv) {
    float t = u_time * 0.1;

    float val1 = sin(uv.x * 2.0 + t) * cos(uv.y * 2.0 - t);
    float val2 = cos(uv.x * 3.0 - t) * sin(uv.y * 3.0 + t);

    vec3 colBase = vec3(0.96, 0.97, 0.98); // crisp cool white
    vec3 colWarm = vec3(0.98, 0.94, 0.93); // rose-water
    vec3 colCool = vec3(0.91, 0.94, 0.96); // soft ice blue

    vec3 bg = mix(colBase, colWarm, smoothstep(-1.0, 1.0, val1));
    bg = mix(bg, colCool, smoothstep(-1.0, 1.0, val2) * 0.5);
    return bg;
}

// First wave — the initial drop. Single damped-sinc from screen
// centre, halved damping (0.5 vs reference's 0.8) and pushed decay
// to (0.6, 1.0) so the tail trails through the splash.
float getInitialElevation(vec2 p) {
    float dist = length(p);
    float waveRadius = u_splash_progress * 3.5;
    float w = (dist - waveRadius) * 5.0;
    float wave = (sin(w) / (1.0 + w * w * 0.5)) * exp(-dist * 0.22);
    float activityLevel = 1.0 - smoothstep(0.6, 1.0, u_splash_progress);
    return wave * activityLevel * 1.8;
}

// Snap burst — localized amplitude spike at the icon's centre at the
// reveal kickoff moment. Decays both spatially (exp(-dist*8)) and
// temporally (exp(-progress*6)) so it's a brief flash that fades into
// the cascade. This is the visual "kick" — the released energy that
// originates the second wave from the icon's collapse.
float getSnapBurst(vec2 p) {
    float dist = length(p);
    return exp(-dist * 8.0) * exp(-u_reveal_progress * 6.0) * 3.0
         * smoothstep(0.0, 0.02, u_reveal_progress);
}

// Gravity well — negative elevation Gaussian bulge at the icon centre,
// scaled by u_gravity_pull. Top-down view of a surface depression: the
// pearl substrate sinks at the centre, refraction picks up the inward
// normal automatically. As u_gravity_pull releases (drops from 1 → 0
// at the snap), this depression fills back in, transitioning the
// elevation at the centre from negative (depressed) → 0 → positive
// (snap burst peak) → outward (cascade). One continuous physical arc.
float getGravityWellElevation(vec2 p) {
    float dist = length(p);
    // Slightly wider falloff (3.5 vs 4.0) + deeper at peak (2.6 vs 1.8)
    // so the substrate depression visibly matches the icon's stronger
    // collapse into a near-point.
    return -exp(-dist * 3.5) * u_gravity_pull * 2.6;
}

// ── Reveal cascade — four staggered waves dissolving the splash
//    iteratively. Each wave is a damped sinc emanating from the icon;
//    together they paint expanding pearl-iridescent rings whose
//    cumulative passage drives the alpha mask.
//
// Three sources of irregularity defeat the "synthetic" feeling:
//   1. Uneven staggers — 0.00, 0.14, 0.31, 0.52 (not periodic)
//   2. Per-wave params — different amplitudes, sin phase offsets
//   3. Angular wobble — each wave's radius is angle-dependent
//
// Each wave's params live in a vec4: x=stagger, y=amplitude,
// z=phase, w=key (used by angleWobble to give each wave a distinct
// noise pattern).
const float REVEAL_RADIUS_SCALE = 4.5;
const float REVEAL_NUM_WAVES = 4.0;

// Pattern matches a released-impulse: one big primary wave from the
// energy release, then progressively smaller echoes. Tight staggers
// (0.00, 0.15, 0.30, 0.45) keep the cascade reading as one continuous
// burst rather than four separate kicks.
const vec4 W0 = vec4(0.00, 4.00, 0.00, 0.0);  // big release
const vec4 W1 = vec4(0.15, 2.50, 1.70, 1.3);  // first echo
const vec4 W2 = vec4(0.30, 1.60, 0.90, 2.6);  // second echo
const vec4 W3 = vec4(0.45, 1.00, 2.40, 3.9);  // last echo

float waveProg(float stagger) {
    return max(0.0, u_reveal_progress - stagger);
}

// Three-harmonic angular noise — distinct per wave (driven by the
// key param), drifting with prog so the wobble itself moves as the
// wave expands. Sum-of-sines amplitudes (0.11+0.07+0.05 = ±0.23 max)
// shift each wave's effective radius noticeably without overpowering
// the base expansion (which is in radius-units, scale 4.5).
float angleWobble(float angle, float key, float prog) {
    float t = prog * 1.2 + key * 11.0;
    return sin(angle * 3.0 + t) * 0.11
         + cos(angle * 5.0 + t * 0.7 - 1.3) * 0.07
         + sin(angle * 2.0 - t * 0.5 + 2.5) * 0.05;
}

float singleRevealWave(vec2 p, vec4 params) {
    float prog = waveProg(params.x);
    if (prog <= 0.0) return 0.0;
    float dist = length(p);
    float angle = atan(p.y, p.x);
    float r = prog * REVEAL_RADIUS_SCALE + angleWobble(angle, params.w, prog);
    float w = (dist - r) * 5.0;
    // Bigger crests (per-wave amplitudes 1.85-2.60) at cinema scale read
    // as proper membrane displacement. sin phase offset varies per wave
    // so the leading edges aren't identical sinc shapes.
    float wave = (sin(w + params.z) / (1.0 + w * w * 0.45)) * params.y;
    return wave * smoothstep(0.0, 0.04, prog);
}

float getRevealElevation(vec2 p) {
    return singleRevealWave(p, W0)
         + singleRevealWave(p, W1)
         + singleRevealWave(p, W2)
         + singleRevealWave(p, W3);
}

// Combined elevation — initial wave + gravity well depression +
// reveal cascade + snap burst. Used both for the normal (driving
// refraction) and for the alpha boundary wiggle below.
float getTotalElevation(vec2 p) {
    return getInitialElevation(p)
         + getGravityWellElevation(p)
         + getRevealElevation(p)
         + getSnapBurst(p);
}

// Combined normal — every elevation source contributes. The snap
// burst's local steep gradient at the icon's centre creates a clear
// refractive bulge at the moment the cascade kicks off.
vec3 getNormal(vec2 p) {
    float e = 0.002;
    float dxR = getTotalElevation(p + vec2(e, 0.0));
    float dxL = getTotalElevation(p - vec2(e, 0.0));
    float dyT = getTotalElevation(p + vec2(0.0, e));
    float dyB = getTotalElevation(p - vec2(0.0, e));
    return normalize(vec3(dxR - dxL, dyT - dyB, e * 3.5));
}

// How much of a given wave has "passed" a pixel (0 = ahead of wave,
// 1 = behind wave). The angle parameter feeds the same angleWobble
// used by singleRevealWave, so the alpha boundary tracks the wave's
// uneven shape rather than a perfect circle.
float wavePassed(float effDist, vec4 params, float angle) {
    float prog = waveProg(params.x);
    if (prog <= 0.0) return 0.0;
    float r = prog * REVEAL_RADIUS_SCALE + angleWobble(angle, params.w, prog);
    return smoothstep(r + 0.25, r - 0.25, effDist);
}

// Iridescent glow along a single wave's front — Gaussian peak that
// diminishes as the wave gets further (so later, larger rings are
// gentler than fresh ones). Tracks the same uneven angular radius.
float waveFrontGlow(float effDist, vec4 params, float angle) {
    float prog = waveProg(params.x);
    if (prog <= 0.0) return 0.0;
    float r = prog * REVEAL_RADIUS_SCALE + angleWobble(angle, params.w, prog);
    float dx = (effDist - r) / 0.22;
    return exp(-dx * dx) * max(0.0, 1.0 - prog * 0.45);
}

vec3 getPearlIridescence(float cosTheta) {
    vec3 a = vec3(0.8, 0.8, 0.8);
    vec3 b = vec3(0.2, 0.2, 0.2);
    vec3 c = vec3(1.0, 1.0, 1.0);
    vec3 d = vec3(0.0, 0.33, 0.67);
    return a + b * cos(6.28318 * (c * cosTheta + d));
}

void main() {
    vec2 uv = gl_FragCoord.xy / u_resolution.xy;
    vec2 p = uv * 2.0 - 1.0;
    p.x *= u_resolution.x / u_resolution.y;

    vec3 viewDir = normalize(vec3(0.0, 0.0, 1.0));
    vec3 normal = getNormal(p);

    // Dense refraction with prismatic chromatic dispersion. Both bumped
    // up during the reveal so the dissolution boundary shows clear
    // rainbow-shifted edges — closer to looking through real liquid.
    float refrStrength = mix(0.12, 0.18, u_reveal_progress);
    float chromaSpread = mix(0.02, 0.06, u_reveal_progress);
    vec2 uvR = uv - normal.xy * refrStrength * (1.0 - chromaSpread);
    vec2 uvG = uv - normal.xy * refrStrength;
    vec2 uvB = uv - normal.xy * refrStrength * (1.0 + chromaSpread);

    vec3 transmittedLight = vec3(
        getStudioBackground(uvR).r,
        getStudioBackground(uvG).g,
        getStudioBackground(uvB).b
    );

    float viewAngle = max(dot(normal, viewDir), 0.0);
    float fresnel = pow(1.0 - viewAngle, 4.0);
    vec3 pearlSheen = getPearlIridescence(viewAngle + u_time * 0.05);

    vec3 lightDir = normalize(vec3(0.2, 0.8, 1.0));
    vec3 halfVector = normalize(lightDir + viewDir);
    float specular = pow(max(dot(normal, halfVector), 0.0), 40.0);
    vec3 highlight = mix(vec3(1.0), pearlSheen, 0.3) * specular * 1.5;

    vec3 finalColor = transmittedLight;
    finalColor += pearlSheen * fresnel * 0.6;
    finalColor += highlight;

    float vignette = length(uv - 0.5) * 0.2;
    finalColor -= vignette;

    // ── REVEAL CASCADE → ALPHA MASK ──────────────────────────────────
    // Four staggered waves dissolve the splash iteratively. Each wave
    // that has passed a pixel contributes 1/4 of an opacity drop, so
    // a pixel goes 1.00 → 0.75 → 0.50 → 0.25 → 0 as the fronts sweep
    // past in turn. Per-wave params (uneven staggers, varied amps,
    // phase offsets, angle wobble keys) defeat the synthetic
    // periodicity of evenly-spaced identical rings.
    //
    // What makes the app underneath read as "warping": effDist
    // subtracts the local wave elevation from radial distance, so the
    // boundary bends inward at troughs and bulges outward at crests.
    // The dissolution edge follows the wiggly membrane surface, not a
    // clean circle. All five elevations (initial + four reveal) stack
    // into the wobble, so the boundary has multi-frequency detail.
    float angle = atan(p.y, p.x);
    float distFromCenter = length(p);
    float waveContrib = getTotalElevation(p) * 0.35;
    float effDist = distFromCenter - waveContrib;

    float passedSum =
        wavePassed(effDist, W0, angle) +
        wavePassed(effDist, W1, angle) +
        wavePassed(effDist, W2, angle) +
        wavePassed(effDist, W3, angle);

    float alpha = 1.0 - passedSum / REVEAL_NUM_WAVES;
    // Closing gate — guarantees alpha lands at 0 by progress=1 even
    // for the corner pixels wave 4 might not have fully cleared.
    alpha *= 1.0 - smoothstep(0.88, 1.0, u_reveal_progress);
    alpha = clamp(alpha, 0.0, 1.0);

    // Four expanding iridescent rings — each wave's leading edge gets
    // its own pearl-tinted glow. Stacks visibly where rings catch up
    // to one another or pass through the same pixel.
    float totalGlow =
        waveFrontGlow(effDist, W0, angle) +
        waveFrontGlow(effDist, W1, angle) +
        waveFrontGlow(effDist, W2, angle) +
        waveFrontGlow(effDist, W3, angle);
    finalColor += pearlSheen * totalGlow * 0.55;

    gl_FragColor = vec4(finalColor, alpha);
}
`

function compile(gl: WebGLRenderingContext, type: number, src: string): WebGLShader | null {
  const shader = gl.createShader(type)
  if (!shader) return null
  gl.shaderSource(shader, src)
  gl.compileShader(shader)
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    console.error('splash shader compile failed:', gl.getShaderInfoLog(shader))
    gl.deleteShader(shader)
    return null
  }
  return shader
}

// Apple's heavy ease-out, with a smoothstep on-ramp over the first 12%
// so the wave's birth doesn't kick like a starting gun.
function smoothEase(t: number): number {
  if (t >= 1) return 1
  if (t <= 0) return 0
  const ramp = Math.min(1, t / 0.12)
  const rampSmooth = ramp * ramp * (3 - 2 * ramp)
  const expoOut = 1 - Math.pow(2, -10 * t)
  return rampSmooth * expoOut
}

// Ease-in-out cubic for the reveal — gentle start, momentum through
// the middle as waves cascade, soft landing as the last wave clears.
// Reads more cinematic than easeOut (which front-loads the dissolution).
function easeInOutCubic(t: number): number {
  if (t <= 0) return 0
  if (t >= 1) return 1
  return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2
}

export function SplashScreen({ onComplete }: { onComplete: () => void }) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const iconWrapRef = useRef<HTMLDivElement>(null)
  // Gravity-well factor 0–1. Updated each frame by the render loop;
  // AnimatedTopographicMark reads it via this same ref. Lives outside
  // React state to avoid 60fps re-renders.
  const gravityPullRef = useRef(0)
  const [revealing, setRevealing] = useState(false)
  const startedAtRef = useRef<number>(performance.now())
  const completedRef = useRef(false)
  const revealStartRef = useRef<number>(0)

  // ── Mark the document while the splash is up so the welcome icon
  //    underneath can stay hidden until the splash unmounts, dodging
  //    the alpha-blend-through-white intermediate.
  useEffect(() => {
    document.body.classList.add('splash-active')
    return () => {
      document.body.classList.remove('splash-active')
    }
  }, [])

  // ── WebGL render loop ────────────────────────────────────────────
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return

    // alpha: true so the shader can output partial alpha during the
    // reveal phase and let the app render through.
    const gl = canvas.getContext('webgl', {
      alpha: true,
      antialias: true,
      premultipliedAlpha: false,
    })
    if (!gl) {
      console.warn('splash: WebGL unavailable, skipping ripple shader')
      return
    }

    const vs = compile(gl, gl.VERTEX_SHADER, VERTEX_SRC)
    const fs = compile(gl, gl.FRAGMENT_SHADER, FRAGMENT_SRC)
    if (!vs || !fs) return

    const program = gl.createProgram()
    if (!program) return
    gl.attachShader(program, vs)
    gl.attachShader(program, fs)
    gl.linkProgram(program)
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      console.error('splash program link failed:', gl.getProgramInfoLog(program))
      return
    }
    gl.useProgram(program)

    const posBuf = gl.createBuffer()
    gl.bindBuffer(gl.ARRAY_BUFFER, posBuf)
    gl.bufferData(
      gl.ARRAY_BUFFER,
      new Float32Array([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1]),
      gl.STATIC_DRAW,
    )
    const posLoc = gl.getAttribLocation(program, 'a_position')
    gl.enableVertexAttribArray(posLoc)
    gl.vertexAttribPointer(posLoc, 2, gl.FLOAT, false, 0, 0)

    const uRes = gl.getUniformLocation(program, 'u_resolution')
    const uTime = gl.getUniformLocation(program, 'u_time')
    const uSplash = gl.getUniformLocation(program, 'u_splash_progress')
    const uReveal = gl.getUniformLocation(program, 'u_reveal_progress')
    const uGravity = gl.getUniformLocation(program, 'u_gravity_pull')

    const resize = () => {
      const dpr = Math.min(window.devicePixelRatio || 1, 2)
      canvas.width = Math.floor(window.innerWidth * dpr)
      canvas.height = Math.floor(window.innerHeight * dpr)
      gl.viewport(0, 0, canvas.width, canvas.height)
      gl.uniform2f(uRes, canvas.width, canvas.height)
    }
    window.addEventListener('resize', resize)
    resize()

    const startTime = performance.now()
    const splashStart = startTime + 300
    let raf = 0
    let disposed = false

    const render = (now: number) => {
      if (disposed) return
      const elapsed = (now - startTime) * 0.001
      const elapsedMs = now - startTime

      const rawProgress = Math.max(0, Math.min(1, (now - splashStart) / RIPPLE_DURATION_MS))
      const progress = smoothEase(rawProgress)

      // Reveal progress — climbs from 0 to 1 across REVEAL_DURATION_MS
      // once revealStartRef is populated. Zero before that, so the
      // alpha mask stays at "everywhere opaque" during phase 1.
      const revealStart = revealStartRef.current
      const revealRaw =
        revealStart > 0
          ? Math.max(0, Math.min(1, (now - revealStart) / REVEAL_DURATION_MS))
          : 0
      const revealEased = easeInOutCubic(revealRaw)

      // ── Icon choreography. Fade-in over 1s, hold at full opacity
      //    through the gravity-well build, then linear fade-out over
      //    the 300ms release window (overlapping with the cascade).
      let iconOpacity = 0
      if (elapsedMs < ICON_FADE_IN_START_MS) {
        iconOpacity = 0
      } else if (elapsedMs < ICON_FADE_IN_END_MS) {
        const t = (elapsedMs - ICON_FADE_IN_START_MS) / (ICON_FADE_IN_END_MS - ICON_FADE_IN_START_MS)
        iconOpacity = t * t * (3 - 2 * t) // smoothstep ease
      } else if (elapsedMs < GRAVITY_PEAK_MS) {
        iconOpacity = 1
      } else if (elapsedMs < RELEASE_END_MS) {
        const t = (elapsedMs - GRAVITY_PEAK_MS) / (RELEASE_END_MS - GRAVITY_PEAK_MS)
        iconOpacity = 1 - t // linear fade-out
      } else {
        iconOpacity = 0
      }
      if (iconWrapRef.current) {
        iconWrapRef.current.style.opacity = String(iconOpacity)
      }

      // Gravity has TWO channels with different release behaviour:
      //
      //   • substrateGp drives the shader's pearl depression. It must
      //     release at the snap so the surface elevation transitions
      //     from negative (depression) → zero → positive (snap burst)
      //     → outward (cascade). That release is the physical engine
      //     of the wave's origin.
      //
      //   • iconGp drives AnimatedTopographicMark's ring compression.
      //     It must NOT release — if it does, the rings expand back to
      //     natural radii during the icon's opacity fade-out, briefly
      //     re-showing the full icon and producing the "flash back"
      //     the user sees. Instead, it stays at peak collapse from
      //     7.5s onward. The icon stays a point and just fades away.
      let substrateGp = 0
      let iconGp = 0
      if (elapsedMs >= GRAVITY_BUILD_START_MS && elapsedMs < GRAVITY_PEAK_MS) {
        const t = (elapsedMs - GRAVITY_BUILD_START_MS) / (GRAVITY_PEAK_MS - GRAVITY_BUILD_START_MS)
        const built = t * t // easeInQuad
        substrateGp = built
        iconGp = built
      } else if (elapsedMs >= GRAVITY_PEAK_MS) {
        // Substrate releases during snap window, then 0.
        if (elapsedMs < RELEASE_END_MS) {
          const t = (elapsedMs - GRAVITY_PEAK_MS) / (RELEASE_END_MS - GRAVITY_PEAK_MS)
          substrateGp = 1 - t * t * (3 - 2 * t) // smoothstep
        }
        // Icon never releases — stays at peak so the rings remain
        // collapsed at a point through the entire opacity fade-out.
        iconGp = 1
      }
      gravityPullRef.current = iconGp

      gl.uniform1f(uTime, elapsed)
      gl.uniform1f(uSplash, progress)
      gl.uniform1f(uReveal, revealEased)
      gl.uniform1f(uGravity, substrateGp)
      gl.drawArrays(gl.TRIANGLES, 0, 6)
      raf = requestAnimationFrame(render)
    }
    raf = requestAnimationFrame(render)

    return () => {
      disposed = true
      cancelAnimationFrame(raf)
      window.removeEventListener('resize', resize)
      gl.deleteBuffer(posBuf)
      gl.deleteShader(vs)
      gl.deleteShader(fs)
      gl.deleteProgram(program)
    }
  }, [])

  // ── Reveal kickoff. Stamps revealStartRef so the WebGL loop picks
  //    it up immediately, flips the `revealing` state to drive the
  //    icon's CSS colour transition, and schedules the unmount for
  //    when the alpha hits zero.
  const beginReveal = () => {
    if (completedRef.current) return
    completedRef.current = true
    revealStartRef.current = performance.now()
    setRevealing(true)
    window.setTimeout(onComplete, REVEAL_DURATION_MS)
  }

  useEffect(() => {
    const t = window.setTimeout(beginReveal, REVEAL_START_MS)
    return () => clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [onComplete])

  // ── Skip on any key / click after the guard period. Skipping fires
  //    beginReveal early, so the user still gets the dissolution
  //    animation — just on a tighter schedule.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (performance.now() - startedAtRef.current < SKIP_GUARD_MS) return
      if (e.key === 'Meta' || e.key === 'Control' || e.key === 'Shift' || e.key === 'Alt') return
      beginReveal()
    }
    const onClick = () => {
      if (performance.now() - startedAtRef.current < SKIP_GUARD_MS) return
      beginReveal()
    }
    window.addEventListener('keydown', onKey)
    window.addEventListener('mousedown', onClick)
    return () => {
      window.removeEventListener('keydown', onKey)
      window.removeEventListener('mousedown', onClick)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div
      className="splash-root fixed inset-0 z-[10000] overflow-hidden"
      style={{
        // No background — the shader's own alpha is the dissolution
        // mechanism. Anything painted here would block the app from
        // showing through the alpha holes.
        backgroundColor: 'transparent',
        // Once revealing has started, let pointer events fall through
        // to the app underneath; the splash is animating away anyway.
        pointerEvents: revealing ? 'none' : 'auto',
      }}
    >
      <canvas
        ref={canvasRef}
        className="splash-canvas absolute inset-0 h-full w-full"
      />
      {/* Splash icon — visible only during the segue (icon fade-in →
          gravity-well stretch → snap fade-out). The welcome icon
          underneath is hidden via body.splash-active throughout; once
          the splash unmounts it fades in cleanly. */}
      <div ref={iconWrapRef} className="splash-icon" style={{ opacity: 0 }}>
        <AnimatedTopographicMark
          size={ICON_SIZE}
          intensity={1}
          gravityPullRef={gravityPullRef}
          className="text-accent"
        />
      </div>
    </div>
  )
}
