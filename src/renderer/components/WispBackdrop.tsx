/**
 * Wisp backdrop — the Three.js particle field from the Morning Room
 * mockup (mockups/morning-room/morning-room-o-letterpress.html), ported
 * to raw WebGL so it ships without pulling in three.js (~600 KB) for one
 * decorative background.
 *
 * The GLSL — domain-warped simplex-noise displacement of a 96k-point
 * grid, soft additive bokeh, diagonal wind drift — is carried over
 * verbatim from the mockup. The three.js scaffolding it replaces is
 * thin: a Points object, a perspective camera with slow sway, and a
 * render loop. Those become a hand-rolled perspective/lookAt pair of
 * mat4s and a rAF loop here.
 *
 * Lifecycle: mounted only while the Morning Room is open (the parent
 * unmounts it on close), so the cleanup that cancels the rAF + drops
 * the GL context runs whenever the room closes. Also pauses when the
 * window is hidden, and honors prefers-reduced-motion (renders a few
 * settled frames, then stops).
 */

import { useEffect, useRef } from 'react'

// Grid dimensions — 400×240 = 96k points, exactly as the mockup.
const GRID_X = 400
const GRID_Y = 240
const SPACING_X = 0.12
const SPACING_Y = 0.15

const GLSL_NOISE = `
  vec4 permute(vec4 x){return mod(((x*34.0)+1.0)*x, 289.0);}
  vec4 taylorInvSqrt(vec4 r){return 1.79284291400159 - 0.85373472095314 * r;}
  float snoise(vec3 v){
    const vec2  C = vec2(1.0/6.0, 1.0/3.0);
    const vec4  D = vec4(0.0, 0.5, 1.0, 2.0);
    vec3 i  = floor(v + dot(v, C.yyy));
    vec3 x0 = v - i + dot(i, C.xxx);
    vec3 g = step(x0.yzx, x0.xyz);
    vec3 l = 1.0 - g;
    vec3 i1 = min(g.xyz, l.zxy);
    vec3 i2 = max(g.xyz, l.zxy);
    vec3 x1 = x0 - i1 + 1.0 * C.xxx;
    vec3 x2 = x0 - i2 + 2.0 * C.xxx;
    vec3 x3 = x0 - 1.0 + 3.0 * C.xxx;
    i = mod(i, 289.0);
    vec4 p = permute(permute(permute(
                i.z + vec4(0.0, i1.z, i2.z, 1.0))
              + i.y + vec4(0.0, i1.y, i2.y, 1.0))
              + i.x + vec4(0.0, i1.x, i2.x, 1.0));
    float n_ = 1.0/7.0;
    vec3  ns = n_ * D.wyz - D.xzx;
    vec4 j = p - 49.0 * floor(p * ns.z * ns.z);
    vec4 x_ = floor(j * ns.z);
    vec4 y_ = floor(j - 7.0 * x_);
    vec4 x = x_ * ns.x + ns.yyyy;
    vec4 y = y_ * ns.x + ns.yyyy;
    vec4 h = 1.0 - abs(x) - abs(y);
    vec4 b0 = vec4(x.xy, y.xy);
    vec4 b1 = vec4(x.zw, y.zw);
    vec4 s0 = floor(b0)*2.0 + 1.0;
    vec4 s1 = floor(b1)*2.0 + 1.0;
    vec4 sh = -step(h, vec4(0.0));
    vec4 a0 = b0.xzyw + s0.xzyw*sh.xxyy;
    vec4 a1 = b1.xzyw + s1.xzyw*sh.zzww;
    vec3 p0 = vec3(a0.xy, h.x);
    vec3 p1 = vec3(a0.zw, h.y);
    vec3 p2 = vec3(a1.xy, h.z);
    vec3 p3 = vec3(a1.zw, h.w);
    vec4 norm = taylorInvSqrt(vec4(dot(p0,p0), dot(p1,p1), dot(p2,p2), dot(p3,p3)));
    p0 *= norm.x; p1 *= norm.y; p2 *= norm.z; p3 *= norm.w;
    vec4 m = max(0.6 - vec4(dot(x0,x0), dot(x1,x1), dot(x2,x2), dot(x3,x3)), 0.0);
    m = m * m;
    return 42.0 * dot(m*m, vec4(dot(p0,x0), dot(p1,x1), dot(p2,x2), dot(p3,x3)));
  }
`

const VERTEX_SHADER = `
  precision highp float;
  ${GLSL_NOISE}
  attribute vec3 position;
  attribute float aRandomSize;
  uniform mat4 modelViewMatrix;
  uniform mat4 projectionMatrix;
  uniform float uTime;
  uniform float uIntroMultiplier;
  varying float vAlpha;
  varying float vElevation;

  void main() {
    vec3 pos = position;
    float tSlow = uTime * 0.015;

    vec3 coord = vec3(
      (pos.x - uTime * 0.5) * 0.022,
      (pos.y - uTime * 0.3) * 0.022,
      pos.z * 0.022
    );

    vec3 q = vec3(
      snoise(coord),
      snoise(coord + vec3(4.1, 1.2, 1.9)),
      snoise(coord + vec3(1.9, 4.1, 7.3))
    );

    vec3 r = vec3(
      snoise(coord + q * 0.9 + vec3(1.2, tSlow,       3.1)),
      snoise(coord + q * 0.9 + vec3(6.3, tSlow * 1.1, 1.9)),
      snoise(coord + q * 0.9 + vec3(1.9, tSlow * 0.9, 5.2))
    );

    pos += r * 10.0 * uIntroMultiplier;

    vec4 mvPosition = modelViewMatrix * vec4(pos, 1.0);
    gl_Position = projectionMatrix * mvPosition;

    float smokeDensity = length(r);
    float sizeBoost = 1.05 + clamp(smokeDensity * 0.2, -0.4, 1.2);
    gl_PointSize = max(3.0, (30.0 / -mvPosition.z) * sizeBoost * aRandomSize);

    float ellipDist = length(pos.xy * vec2(0.68, 1.15));
    float radialFade = smoothstep(34.0, 16.0, ellipDist);

    vAlpha = (0.08 + smokeDensity * 0.24) * radialFade;
    vElevation = pos.y;
  }
`

const FRAGMENT_SHADER = `
  precision highp float;
  varying float vAlpha;
  varying float vElevation;

  void main() {
    float dist = distance(gl_PointCoord, vec2(0.5));
    if (dist > 0.5) discard;

    float core = smoothstep(0.06, 0.0, dist) * 1.5;
    float halo = smoothstep(0.5, 0.06, dist) * 0.75;
    float alpha = (core + halo) * vAlpha;

    vec3 hotBase = vec3(0.84, 0.92, 1.00);
    vec3 coolTop = vec3(0.42, 0.50, 0.62);
    float factor = clamp((vElevation + 12.0) / 24.0, 0.0, 1.0);
    vec3 finalColor = mix(hotBase, coolTop, factor);

    gl_FragColor = vec4(finalColor, alpha);
  }
`

// ─── mat4 helpers (column-major, WebGL convention) ──────────────────

function perspective(fovyRad: number, aspect: number, near: number, far: number): Float32Array {
  const f = 1 / Math.tan(fovyRad / 2)
  const nf = 1 / (near - far)
  const out = new Float32Array(16)
  out[0] = f / aspect
  out[5] = f
  out[10] = (far + near) * nf
  out[11] = -1
  out[14] = 2 * far * near * nf
  return out
}

function lookAt(
  ex: number, ey: number, ez: number,
  cx: number, cy: number, cz: number,
  ux: number, uy: number, uz: number,
): Float32Array {
  // z = normalize(eye - center)
  let zx = ex - cx, zy = ey - cy, zz = ez - cz
  let len = Math.hypot(zx, zy, zz) || 1
  zx /= len; zy /= len; zz /= len
  // x = normalize(cross(up, z))
  let xx = uy * zz - uz * zy
  let xy = uz * zx - ux * zz
  let xz = ux * zy - uy * zx
  len = Math.hypot(xx, xy, xz) || 1
  xx /= len; xy /= len; xz /= len
  // y = cross(z, x)
  const yx = zy * xz - zz * xy
  const yy = zz * xx - zx * xz
  const yz = zx * xy - zy * xx
  const out = new Float32Array(16)
  out[0] = xx; out[1] = yx; out[2] = zx; out[3] = 0
  out[4] = xy; out[5] = yy; out[6] = zy; out[7] = 0
  out[8] = xz; out[9] = yz; out[10] = zz; out[11] = 0
  out[12] = -(xx * ex + xy * ey + xz * ez)
  out[13] = -(yx * ex + yy * ey + yz * ez)
  out[14] = -(zx * ex + zy * ey + zz * ez)
  out[15] = 1
  return out
}

function compile(gl: WebGLRenderingContext, type: number, src: string): WebGLShader | null {
  const sh = gl.createShader(type)
  if (!sh) return null
  gl.shaderSource(sh, src)
  gl.compileShader(sh)
  if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
    console.warn('[wisp] shader compile failed:', gl.getShaderInfoLog(sh))
    gl.deleteShader(sh)
    return null
  }
  return sh
}

export function WispBackdrop() {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return

    const gl = (canvas.getContext('webgl', {
      antialias: true,
      alpha: false,
      depth: false,
      premultipliedAlpha: false,
    }) || canvas.getContext('experimental-webgl')) as WebGLRenderingContext | null
    if (!gl) {
      // No WebGL — the overlay's solid background shows through, fine.
      return
    }

    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches

    // ── Program ──
    const vs = compile(gl, gl.VERTEX_SHADER, VERTEX_SHADER)
    const fs = compile(gl, gl.FRAGMENT_SHADER, FRAGMENT_SHADER)
    if (!vs || !fs) return
    const prog = gl.createProgram()
    if (!prog) return
    gl.attachShader(prog, vs)
    gl.attachShader(prog, fs)
    gl.linkProgram(prog)
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
      console.warn('[wisp] program link failed:', gl.getProgramInfoLog(prog))
      return
    }
    gl.useProgram(prog)

    // ── Geometry ── grid of points with per-axis jitter + random size.
    const count = GRID_X * GRID_Y
    const positions = new Float32Array(count * 3)
    const sizes = new Float32Array(count)
    for (let i = 0; i < GRID_X; i++) {
      for (let j = 0; j < GRID_Y; j++) {
        const k = i * GRID_Y + j
        const jx = (Math.random() - 0.5) * SPACING_X * 0.95
        const jy = (Math.random() - 0.5) * SPACING_Y * 0.95
        const jz = (Math.random() - 0.5) * 0.2
        positions[k * 3] = (i - GRID_X / 2) * SPACING_X + jx
        positions[k * 3 + 1] = (j - GRID_Y / 2) * SPACING_Y + jy
        positions[k * 3 + 2] = jz
        sizes[k] = 0.4 + Math.random() * 0.6
      }
    }

    const posBuf = gl.createBuffer()
    gl.bindBuffer(gl.ARRAY_BUFFER, posBuf)
    gl.bufferData(gl.ARRAY_BUFFER, positions, gl.STATIC_DRAW)
    const aPos = gl.getAttribLocation(prog, 'position')
    gl.enableVertexAttribArray(aPos)
    gl.vertexAttribPointer(aPos, 3, gl.FLOAT, false, 0, 0)

    const sizeBuf = gl.createBuffer()
    gl.bindBuffer(gl.ARRAY_BUFFER, sizeBuf)
    gl.bufferData(gl.ARRAY_BUFFER, sizes, gl.STATIC_DRAW)
    const aSize = gl.getAttribLocation(prog, 'aRandomSize')
    gl.enableVertexAttribArray(aSize)
    gl.vertexAttribPointer(aSize, 1, gl.FLOAT, false, 0, 0)

    const uMV = gl.getUniformLocation(prog, 'modelViewMatrix')
    const uProj = gl.getUniformLocation(prog, 'projectionMatrix')
    const uTime = gl.getUniformLocation(prog, 'uTime')
    const uIntro = gl.getUniformLocation(prog, 'uIntroMultiplier')

    // ── GL state: additive blend, no depth (additive is order-free). ──
    gl.disable(gl.DEPTH_TEST)
    gl.enable(gl.BLEND)
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE)
    gl.clearColor(6 / 255, 7 / 255, 11 / 255, 1)

    // ── Resize ──
    let aspect = 1
    const resize = () => {
      const dpr = Math.min(window.devicePixelRatio || 1, 2)
      const w = canvas.clientWidth || window.innerWidth
      const h = canvas.clientHeight || window.innerHeight
      canvas.width = Math.max(1, Math.floor(w * dpr))
      canvas.height = Math.max(1, Math.floor(h * dpr))
      aspect = canvas.width / canvas.height
      gl.viewport(0, 0, canvas.width, canvas.height)
    }
    resize()
    window.addEventListener('resize', resize)

    const FOV = (45 * Math.PI) / 180
    const proj = () => perspective(FOV, aspect, 0.1, 500)

    let raf = 0
    let running = true
    const start = performance.now()

    const renderFrame = (t: number) => {
      // Intro ramp: 0→1 over 7s, smoothstep.
      const tt = Math.min(1, (performance.now() - start) / 7000)
      gl.uniform1f(uIntro, tt * tt * (3 - 2 * tt))
      gl.uniform1f(uTime, t)

      // Slow camera sway around the origin.
      const ex = Math.sin(t * 0.035) * 2.5
      const ey = 1.5 + Math.cos(t * 0.025) * 1.0
      const ez = 21.0 + Math.sin(t * 0.02) * 1.5
      gl.uniformMatrix4fv(uMV, false, lookAt(ex, ey, ez, 0, 0, 0, 0, 1, 0))
      gl.uniformMatrix4fv(uProj, false, proj())

      gl.clear(gl.COLOR_BUFFER_BIT)
      gl.drawArrays(gl.POINTS, 0, count)
    }

    const loop = () => {
      if (!running) return
      renderFrame((performance.now() - start) / 1000)
      raf = requestAnimationFrame(loop)
    }

    if (reduced) {
      // Render a handful of advancing frames so the field settles into a
      // wispy still, then stop — no continuous animation.
      let n = 0
      const settle = () => {
        renderFrame(n * 0.4)
        if (n++ < 60) requestAnimationFrame(settle)
      }
      requestAnimationFrame(settle)
    } else {
      raf = requestAnimationFrame(loop)
    }

    // Pause the loop while the window is hidden (saves GPU/battery).
    const onVisibility = () => {
      if (reduced) return
      if (document.hidden) {
        running = false
        cancelAnimationFrame(raf)
      } else if (!running) {
        running = true
        raf = requestAnimationFrame(loop)
      }
    }
    document.addEventListener('visibilitychange', onVisibility)

    return () => {
      running = false
      cancelAnimationFrame(raf)
      window.removeEventListener('resize', resize)
      document.removeEventListener('visibilitychange', onVisibility)
      try {
        gl.deleteBuffer(posBuf)
        gl.deleteBuffer(sizeBuf)
        gl.deleteProgram(prog)
        gl.deleteShader(vs)
        gl.deleteShader(fs)
        gl.getExtension('WEBGL_lose_context')?.loseContext()
      } catch {
        /* context already gone */
      }
    }
  }, [])

  // Inline layout duplicates .mroom-wisp so the canvas is full-viewport
  // even on the very first measured frame, before the injected <style>
  // applies. width/height:100% are load-bearing — a <canvas> is a
  // replaced element and won't stretch from inset/top-left alone.
  return (
    <canvas
      ref={canvasRef}
      className="mroom-wisp"
      aria-hidden="true"
      style={{
        position: 'fixed',
        top: 0,
        left: 0,
        width: '100%',
        height: '100%',
        zIndex: -1,
        display: 'block',
        pointerEvents: 'none',
      }}
    />
  )
}
