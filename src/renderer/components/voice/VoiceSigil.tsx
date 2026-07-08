import { useEffect, useRef } from 'react'
import type { VoiceEngineState } from '../../voice/engine'

/**
 * VoiceSigil — the Galdr presence mark (docs/freyja-voice-galdr.html §03).
 *
 * A live 3D particle cloud rendered in WebGL2: a few hundred points on a
 * noise-breathing sphere, additively blended into a steel-blue nebula
 * that rotates in space. Every behaviour maps 1:1 to the real pipeline
 * state — the sigil is never decorative:
 *
 *   idle        loose, dim cloud drifting slowly — mic is OFF
 *   minting /
 *   connecting  scattered points coalesce inward into the formed cloud
 *   listening   the cloud swells and shimmers with the LIVE mic RMS
 *   thinking    points pull into a dense, fast-spinning core
 *   acting      a bright shell of energy pulses outward, tools firing
 *   speaking    concentric ripples travel out through the cloud
 *   error       the cloud flushes red and scatters, then settles
 *   closing     the cloud dims and disperses as the mic goes cold
 *
 * State targets are smoothed (attack/release) so transitions glide
 * rather than snap. The rAF loop only runs while the canvas is actually
 * on screen (IntersectionObserver + document visibility); reduced-motion
 * renders one static, representative frame.
 *
 * WebGL2 is the primary path. Where it is unavailable — jsdom under test,
 * a software-GL headless shell — the component silently falls back to the
 * original DPR-aware canvas contour rendering so nothing ever throws.
 */

// Palette — pinned design tokens (tailwind.config.cjs).
const ACCENT_RGB = [0.658, 0.831, 0.988] // #a8d4fc
const ACCENT_HI_RGB = [0.769, 0.878, 0.988] // #c4e0fc
const DANGER_RGB = [0.706, 0.51, 0.51] // #b48282

// ── the per-state uniform targets ──────────────────────────────────────────
// Each field is lerped toward its target every frame. Fields:
//   energy  overall liveliness (point size + noise amplitude)
//   spin    rotation speed
//   contract  pull toward the core (thinking)
//   expand  push the shell outward (listening/speaking)
//   burst   acting energy shell — >0 enables the travelling pulse
//   wave    speaking ripples
//   danger  0 = steel blue, 1 = flushed red
//   form    1 = formed sphere, 0 = scattered to the winds
//   bright  base alpha of every point
type Targets = {
  energy: number
  spin: number
  contract: number
  expand: number
  burst: number
  wave: number
  danger: number
  form: number
  bright: number
}

const BASE: Targets = {
  energy: 0.15,
  spin: 0.08,
  contract: 0,
  expand: 0,
  burst: 0,
  wave: 0,
  danger: 0,
  form: 1,
  bright: 0.5,
}

function targetsFor(state: VoiceEngineState): Targets {
  switch (state) {
    case 'idle':
      return { ...BASE, energy: 0.15, spin: 0.07, bright: 0.4 }
    case 'minting':
      return { ...BASE, energy: 0.5, spin: 0.28, form: 0.1, bright: 0.7 }
    case 'connecting':
      return { ...BASE, energy: 0.6, spin: 0.34, form: 0.55, bright: 0.75 }
    case 'listening':
      return { ...BASE, energy: 0.55, spin: 0.14, expand: 0.12, bright: 0.7 }
    case 'thinking':
      return { ...BASE, energy: 0.75, spin: 0.62, contract: 0.5, bright: 0.72 }
    case 'acting':
      return { ...BASE, energy: 0.95, spin: 0.4, burst: 1, bright: 0.85 }
    case 'speaking':
      return { ...BASE, energy: 0.72, spin: 0.18, expand: 0.1, wave: 1, bright: 0.78 }
    case 'error':
      return { ...BASE, energy: 0.55, spin: 0.3, danger: 1, form: 0.55, bright: 0.75 }
    case 'closing':
      return { ...BASE, energy: 0.1, spin: 0.05, form: 0.45, bright: 0.3 }
    default:
      return { ...BASE }
  }
}

// ── shaders ─────────────────────────────────────────────────────────────────
// 3D simplex noise (Ashima / webgl-noise, public domain) gives the cloud its
// organic breathing surface; the rest of the vertex program is the state math.
const VERT = `#version 300 es
precision highp float;
in vec3 a_base;      // unit-sphere position (fibonacci)
in float a_seed;     // per-point random 0..1
uniform float u_time, u_dpr, u_pt;
uniform float u_energy, u_spin, u_contract, u_expand, u_burst, u_wave, u_form, u_level;
out float v_bright;
out float v_depth;

vec3 mod289(vec3 x){return x-floor(x*(1.0/289.0))*289.0;}
vec4 mod289(vec4 x){return x-floor(x*(1.0/289.0))*289.0;}
vec4 permute(vec4 x){return mod289(((x*34.0)+1.0)*x);}
vec4 taylorInvSqrt(vec4 r){return 1.79284291400159-0.85373472095314*r;}
float snoise(vec3 v){
  const vec2 C=vec2(1.0/6.0,1.0/3.0); const vec4 D=vec4(0.0,0.5,1.0,2.0);
  vec3 i=floor(v+dot(v,C.yyy)); vec3 x0=v-i+dot(i,C.xxx);
  vec3 g=step(x0.yzx,x0.xyz); vec3 l=1.0-g; vec3 i1=min(g.xyz,l.zxy); vec3 i2=max(g.xyz,l.zxy);
  vec3 x1=x0-i1+C.xxx; vec3 x2=x0-i2+C.yyy; vec3 x3=x0-D.yyy;
  i=mod289(i);
  vec4 p=permute(permute(permute(i.z+vec4(0.0,i1.z,i2.z,1.0))+i.y+vec4(0.0,i1.y,i2.y,1.0))+i.x+vec4(0.0,i1.x,i2.x,1.0));
  float n_=0.142857142857; vec3 ns=n_*D.wyz-D.xzx;
  vec4 j=p-49.0*floor(p*ns.z*ns.z);
  vec4 x_=floor(j*ns.z); vec4 y_=floor(j-7.0*x_);
  vec4 x=x_*ns.x+ns.yyyy; vec4 y=y_*ns.x+ns.yyyy; vec4 h=1.0-abs(x)-abs(y);
  vec4 b0=vec4(x.xy,y.xy); vec4 b1=vec4(x.zw,y.zw);
  vec4 s0=floor(b0)*2.0+1.0; vec4 s1=floor(b1)*2.0+1.0; vec4 sh=-step(h,vec4(0.0));
  vec4 a0=b0.xzyw+s0.xzyw*sh.xxyy; vec4 a1=b1.xzyw+s1.xzyw*sh.zzww;
  vec3 p0=vec3(a0.xy,h.x); vec3 p1=vec3(a0.zw,h.y); vec3 p2=vec3(a1.xy,h.z); vec3 p3=vec3(a1.zw,h.w);
  vec4 norm=taylorInvSqrt(vec4(dot(p0,p0),dot(p1,p1),dot(p2,p2),dot(p3,p3)));
  p0*=norm.x; p1*=norm.y; p2*=norm.z; p3*=norm.w;
  vec4 m=max(0.6-vec4(dot(x0,x0),dot(x1,x1),dot(x2,x2),dot(x3,x3)),0.0); m=m*m;
  return 42.0*dot(m*m,vec4(dot(p0,x0),dot(p1,x1),dot(p2,x2),dot(p3,x3)));
}
mat3 rotY(float a){float c=cos(a),s=sin(a);return mat3(c,0.0,s,0.0,1.0,0.0,-s,0.0,c);}
mat3 rotX(float a){float c=cos(a),s=sin(a);return mat3(1.0,0.0,0.0,0.0,c,-s,0.0,s,c);}
float rnd(float x){return fract(sin(x*127.1)*43758.5453);}

void main(){
  vec3 base = rotX(0.5) * rotY(u_time * u_spin) * a_base;
  float n = snoise(base * 1.7 + vec3(0.0, u_time * 0.16, a_seed));
  float polar = a_base.y * 0.5 + 0.5;                 // 0..1 pole-to-pole

  float radius = 1.0;
  radius += n * (0.10 + 0.20 * u_energy);
  radius += u_level * 0.40 * (0.5 + 0.5 * n);          // mic swell
  radius += u_expand * 0.22;
  radius -= u_contract * 0.42;

  // acting: a shell of energy sweeps pole-to-pole and back
  float bph = fract(u_time * 0.9);
  float band = smoothstep(0.10, 0.0, abs(polar - bph));
  radius += band * u_burst * 0.45;

  // speaking: concentric ripples travelling outward through the cloud
  radius += sin(polar * 11.0 - u_time * 3.2) * u_wave * 0.12;

  vec3 p = base * radius;

  // form<1: scatter each point outward along its own random direction
  vec3 dir = normalize(vec3(rnd(a_seed) - 0.5, rnd(a_seed + 1.7) - 0.5, rnd(a_seed + 3.1) - 0.5));
  p = mix(p + dir * (1.6 + a_seed * 3.0), p, clamp(u_form, 0.0, 1.0));

  // simple perspective — nearer points read larger + brighter
  float persp = 3.4;
  float sc = persp / (persp - p.z);
  gl_Position = vec4(p.xy * sc * 0.52, 0.0, 1.0);
  gl_PointSize = u_pt * u_dpr * sc * (0.55 + 0.7 * u_energy) * (0.7 + 0.6 * rnd(a_seed + 7.0));
  v_depth = sc;
  v_bright = 0.35 + 0.65 * smoothstep(0.6, 1.4, sc) + 0.25 * rnd(a_seed + 9.0);
}`

const FRAG = `#version 300 es
precision highp float;
in float v_bright;
in float v_depth;
uniform float u_bright, u_danger;
uniform vec3 u_accent, u_accentHi, u_danger3;
out vec4 fragColor;
void main(){
  vec2 pc = gl_PointCoord - 0.5;
  float d = length(pc);
  float a = smoothstep(0.5, 0.0, d);        // soft round falloff
  a *= a;
  vec3 blue = mix(u_accent, u_accentHi, clamp(v_bright, 0.0, 1.0));
  vec3 col = mix(blue, u_danger3, u_danger);
  float alpha = a * u_bright * (0.45 + 0.55 * v_bright);
  fragColor = vec4(col * alpha, alpha);      // additive (blend ONE, ONE)
}`

function compile(gl: WebGL2RenderingContext, type: number, src: string): WebGLShader | null {
  const sh = gl.createShader(type)
  if (!sh) return null
  gl.shaderSource(sh, src)
  gl.compileShader(sh)
  if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
    gl.deleteShader(sh)
    return null
  }
  return sh
}

function fibonacciSphere(n: number): Float32Array {
  const out = new Float32Array(n * 3)
  const phi = Math.PI * (3 - Math.sqrt(5))
  for (let i = 0; i < n; i++) {
    const y = 1 - (i / (n - 1)) * 2
    const r = Math.sqrt(Math.max(0, 1 - y * y))
    const th = phi * i
    out[i * 3] = Math.cos(th) * r
    out[i * 3 + 1] = y
    out[i * 3 + 2] = Math.sin(th) * r
  }
  return out
}

export function VoiceSigil({
  size,
  state,
  level = 0,
  onClick,
  className = '',
}: {
  size: number
  state: VoiceEngineState
  level?: number
  onClick?: () => void
  className?: string
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const stateRef = useRef<VoiceEngineState>(state)
  const levelRef = useRef(0)
  levelRef.current = Math.max(0, Math.min(1, level))

  useEffect(() => {
    stateRef.current = state
  }, [state])

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return

    const dpr = Math.max(1, Math.min(2.5, window.devicePixelRatio || 1))
    canvas.width = Math.round(size * dpr)
    canvas.height = Math.round(size * dpr)

    const reducedMotion = !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

    // ── try WebGL2; fall back to the canvas contour sigil on failure ───────
    const gl = canvas.getContext('webgl2', {
      alpha: true,
      premultipliedAlpha: false,
      antialias: true,
      depth: false,
    }) as WebGL2RenderingContext | null

    if (!gl) return mountCanvasFallback(canvas, size, dpr, stateRef, levelRef, reducedMotion)

    const vs = compile(gl, gl.VERTEX_SHADER, VERT)
    const fs = compile(gl, gl.FRAGMENT_SHADER, FRAG)
    const prog = vs && fs ? gl.createProgram() : null
    if (!vs || !fs || !prog) return mountCanvasFallback(canvas, size, dpr, stateRef, levelRef, reducedMotion)
    gl.attachShader(prog, vs)
    gl.attachShader(prog, fs)
    gl.linkProgram(prog)
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
      gl.deleteProgram(prog)
      return mountCanvasFallback(canvas, size, dpr, stateRef, levelRef, reducedMotion)
    }

    const count = size <= 30 ? 520 : 1500
    const basePos = fibonacciSphere(count)
    const seeds = new Float32Array(count)
    for (let i = 0; i < count; i++) seeds[i] = Math.random()

    const vao = gl.createVertexArray()
    gl.bindVertexArray(vao)
    const posBuf = gl.createBuffer()
    gl.bindBuffer(gl.ARRAY_BUFFER, posBuf)
    gl.bufferData(gl.ARRAY_BUFFER, basePos, gl.STATIC_DRAW)
    const aBase = gl.getAttribLocation(prog, 'a_base')
    gl.enableVertexAttribArray(aBase)
    gl.vertexAttribPointer(aBase, 3, gl.FLOAT, false, 0, 0)
    const seedBuf = gl.createBuffer()
    gl.bindBuffer(gl.ARRAY_BUFFER, seedBuf)
    gl.bufferData(gl.ARRAY_BUFFER, seeds, gl.STATIC_DRAW)
    const aSeed = gl.getAttribLocation(prog, 'a_seed')
    gl.enableVertexAttribArray(aSeed)
    gl.vertexAttribPointer(aSeed, 1, gl.FLOAT, false, 0, 0)

    const U = (n: string) => gl.getUniformLocation(prog, n)
    const uni = {
      time: U('u_time'), dpr: U('u_dpr'), pt: U('u_pt'),
      energy: U('u_energy'), spin: U('u_spin'), contract: U('u_contract'),
      expand: U('u_expand'), burst: U('u_burst'), wave: U('u_wave'),
      form: U('u_form'), level: U('u_level'), bright: U('u_bright'),
      danger: U('u_danger'), accent: U('u_accent'), accentHi: U('u_accentHi'),
      danger3: U('u_danger3'),
    }

    gl.useProgram(prog)
    gl.uniform1f(uni.dpr, dpr)
    gl.uniform1f(uni.pt, size <= 30 ? 1.7 : 2.2)
    gl.uniform3fv(uni.accent, ACCENT_RGB)
    gl.uniform3fv(uni.accentHi, ACCENT_HI_RGB)
    gl.uniform3fv(uni.danger3, DANGER_RGB)
    gl.enable(gl.BLEND)
    gl.blendFunc(gl.ONE, gl.ONE) // additive glow on the transparent canvas
    gl.viewport(0, 0, canvas.width, canvas.height)

    // Smoothed current uniform state (lerped toward the per-state target).
    const cur: Targets = { ...targetsFor(state) }
    // The shader rotates by u_time * u_spin. Feeding wall-clock time would
    // snap the angle whenever spin changes, so we integrate our own
    // rotation phase (∫ spin dt) and hand THAT to the shader as u_time,
    // with u_spin pinned to 1. The noise field also reads u_time, so it
    // ends up coupled to rotation speed — which reads as the cloud
    // "breathing faster when it thinks", a happy accident worth keeping.
    let rotPhase = 0
    let smoothLvl = 0

    const render = (dt: number, staticFrame: boolean): void => {
      const tgt = targetsFor(staticFrame ? 'listening' : stateRef.current)
      const k = staticFrame ? 1 : Math.min(1, dt * 4.5) // attack/release
      for (const key of Object.keys(cur) as (keyof Targets)[]) {
        cur[key] += (tgt[key] - cur[key]) * k
      }
      const rawLvl = staticFrame ? 0.4 : levelRef.current
      smoothLvl += (rawLvl - smoothLvl) * (rawLvl > smoothLvl ? 0.5 : 0.15)
      rotPhase += (staticFrame ? 0.18 : cur.spin) * (staticFrame ? 8 : dt) + dt * 0.4

      gl.clear(gl.COLOR_BUFFER_BIT)
      gl.useProgram(prog)
      gl.bindVertexArray(vao)
      gl.uniform1f(uni.time, rotPhase)
      gl.uniform1f(uni.spin, 1.0)
      gl.uniform1f(uni.energy, cur.energy)
      gl.uniform1f(uni.contract, cur.contract)
      gl.uniform1f(uni.expand, cur.expand)
      gl.uniform1f(uni.burst, cur.burst)
      gl.uniform1f(uni.wave, cur.wave)
      gl.uniform1f(uni.form, cur.form)
      // Mic swell only reads in listening — other states ignore the feed.
      const listening = staticFrame || stateRef.current === 'listening'
      gl.uniform1f(uni.level, listening ? smoothLvl : 0)
      gl.uniform1f(uni.bright, cur.bright)
      gl.uniform1f(uni.danger, cur.danger)
      gl.drawArrays(gl.POINTS, 0, count)
    }

    let raf = 0
    let running = false
    let inView = true
    let last = performance.now()

    const cleanupGl = () => {
      gl.deleteProgram(prog)
      gl.deleteShader(vs)
      gl.deleteShader(fs)
      gl.deleteBuffer(posBuf)
      gl.deleteBuffer(seedBuf)
      gl.deleteVertexArray(vao)
    }

    if (reducedMotion) {
      render(0, true)
      return cleanupGl
    }

    const loop = (now: number) => {
      if (!running) return
      const dt = Math.min(0.05, (now - last) / 1000)
      last = now
      render(dt, false)
      raf = requestAnimationFrame(loop)
    }
    const sync = () => {
      const shouldRun = inView && document.visibilityState !== 'hidden'
      if (shouldRun && !running) {
        running = true
        last = performance.now()
        raf = requestAnimationFrame(loop)
      } else if (!shouldRun && running) {
        running = false
        cancelAnimationFrame(raf)
      }
    }
    const observer = new IntersectionObserver(([entry]) => {
      inView = entry.isIntersecting
      sync()
    })
    observer.observe(canvas)
    const onVis = () => sync()
    document.addEventListener('visibilitychange', onVis)
    sync()

    return () => {
      running = false
      cancelAnimationFrame(raf)
      observer.disconnect()
      document.removeEventListener('visibilitychange', onVis)
      cleanupGl()
    }
  }, [size])

  return (
    <canvas
      ref={canvasRef}
      style={{ width: size, height: size }}
      className={`${onClick ? 'cursor-pointer ' : ''}${className}`}
      onClick={onClick}
      role={onClick ? 'button' : undefined}
      aria-label={onClick ? `voice — ${state}` : undefined}
      aria-hidden={onClick ? undefined : true}
    />
  )
}

// ── Canvas2D fallback ───────────────────────────────────────────────────────
// The original contour-ring sigil, kept intact for environments without
// WebGL2 (jsdom under test, software-GL headless). Same state vocabulary.
const DIM_RGB = '110,110,110'
const ACC = '168,212,252'
const ACC_HI = '196,224,252'
const WARM = '184,160,120'
const DANG = '180,130,130'

function mountCanvasFallback(
  canvas: HTMLCanvasElement,
  size: number,
  dpr: number,
  stateRef: { current: VoiceEngineState },
  levelRef: { current: number },
  reducedMotion: boolean,
): (() => void) | void {
  const ctx = canvas.getContext('2d')
  if (!ctx) return
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  const RINGS = 5
  const STEPS = 44
  const cx = size / 2
  const cy = size / 2
  const R = size * 0.44
  const lw = size <= 28 ? 1 : 1.3
  let smooth = 0

  const wobble = (a: number, ring: number, drift: number): number =>
    Math.sin(a * 3 + ring * 0.34 + drift) * 0.52 +
    Math.cos(a * 5 - ring * 0.2 - drift * 0.7) * 0.26 +
    Math.sin(a * 2 + ring * 0.5 + 1.2) * 0.34

  const draw = (t: number, staticFrame = false): void => {
    const s = stateRef.current
    ctx.clearRect(0, 0, size, size)
    const target = levelRef.current
    smooth += (target - smooth) * (target > smooth ? 0.5 : 0.15)
    const lvl = staticFrame ? 0.45 : smooth
    for (let i = 0; i < RINGS; i++) {
      const tr = (i + 1) / RINGS
      let r = R * tr
      let amp = 0.09
      let alpha = 0.16
      let col = ACC
      let drift = 0
      switch (s) {
        case 'idle':
          col = DIM_RGB
          r *= 1 + 0.02 * Math.sin((t / 8) * Math.PI * 2)
          alpha = 0.13 + 0.04 * Math.sin((t / 8) * Math.PI * 2 + i * 0.7)
          break
        case 'minting':
        case 'connecting': {
          const k = ((staticFrame ? 0.7 : t) % 1.4) / 1.4
          const bloom = Math.sin(Math.PI * Math.max(0, Math.min(1, k * 1.8 - tr * 0.8)))
          r *= 1 + 0.09 * bloom
          alpha = 0.1 + 0.32 * bloom
          break
        }
        case 'listening':
          amp = (0.07 + 0.12 * lvl) * (0.4 + 0.6 * tr)
          alpha = 0.2 + 0.32 * lvl
          drift = t * 5
          break
        case 'thinking':
          r *= 0.92
          alpha = 0.2
          drift = (i % 2 === 0 ? 1 : -1) * t * 0.6
          break
        case 'acting':
          drift = t * 0.3
          break
        case 'speaking': {
          const ph = ((staticFrame ? 0.4 : t) * 0.55 + i / RINGS) % 1
          r *= 1 + 0.06 * ph
          alpha = 0.05 + 0.3 * (1 - ph)
          break
        }
        case 'error':
          col = DANG
          alpha = 0.12
          break
        case 'closing':
          col = DIM_RGB
          r *= 0.96
          alpha = 0.1
          break
      }
      ctx.beginPath()
      for (let sIdx = 0; sIdx <= STEPS; sIdx++) {
        const a = (sIdx / STEPS) * Math.PI * 2
        const rr = r * (1 + wobble(a, i, drift) * amp)
        const px = cx + Math.cos(a) * rr
        const py = cy + Math.sin(a) * rr
        if (sIdx === 0) ctx.moveTo(px, py)
        else ctx.lineTo(px, py)
      }
      ctx.closePath()
      ctx.strokeStyle = `rgba(${col},${alpha.toFixed(3)})`
      ctx.lineWidth = lw
      ctx.stroke()
    }
    if (s === 'acting') {
      const a0 = staticFrame ? 0.8 : t * 2.4
      ctx.beginPath()
      ctx.strokeStyle = `rgba(${ACC_HI},0.85)`
      ctx.lineWidth = lw * 1.5
      ctx.arc(cx, cy, R, a0, a0 + 0.9)
      ctx.stroke()
      ctx.beginPath()
      ctx.strokeStyle = `rgba(${WARM},0.5)`
      ctx.lineWidth = lw * 1.1
      ctx.arc(cx, cy, R * 0.62, -a0 * 0.7, -a0 * 0.7 + 0.55)
      ctx.stroke()
    }
    const dotBase = Math.max(1.2, size * 0.045)
    const dotR = s === 'listening' ? dotBase + lvl * size * 0.045 : dotBase
    ctx.fillStyle =
      s === 'idle' || s === 'closing'
        ? `rgba(${DIM_RGB},0.7)`
        : s === 'error'
          ? `rgba(${DANG},0.9)`
          : `rgba(${ACC_HI},0.9)`
    ctx.beginPath()
    ctx.arc(cx, cy, dotR, 0, Math.PI * 2)
    ctx.fill()
  }

  if (reducedMotion) {
    draw(0.7, true)
    return
  }
  let raf = 0
  let running = false
  let inView = true
  const t0 = performance.now()
  const loop = (now: number) => {
    if (!running) return
    draw((now - t0) / 1000)
    raf = requestAnimationFrame(loop)
  }
  const sync = () => {
    const shouldRun = inView && document.visibilityState !== 'hidden'
    if (shouldRun && !running) {
      running = true
      raf = requestAnimationFrame(loop)
    } else if (!shouldRun && running) {
      running = false
      cancelAnimationFrame(raf)
    }
  }
  const observer = new IntersectionObserver(([entry]) => {
    inView = entry.isIntersecting
    sync()
  })
  observer.observe(canvas)
  const onVis = () => sync()
  document.addEventListener('visibilitychange', onVis)
  sync()
  return () => {
    running = false
    cancelAnimationFrame(raf)
    observer.disconnect()
    document.removeEventListener('visibilitychange', onVis)
  }
}
