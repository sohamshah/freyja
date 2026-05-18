import { useEffect, useRef } from 'react'

/** Animated WebGL fabric behind the DONE section.
 *
 *  A handful of overlapping sine waves drive a luminance field; an
 *  ordered Bayer-matrix dither threshold turns that into a 1-bit
 *  twinkle pattern that drifts across the surface. Reads as a
 *  slightly metallic / satin "completed work shimmer" without
 *  competing with the cards in front. Output alpha stays low and the
 *  whole canvas sits behind the cards via `pointer-events: none`.
 *
 *  Stops the rAF loop when the canvas is off-screen
 *  (IntersectionObserver) and when the OS reports
 *  prefers-reduced-motion. Resizes via ResizeObserver on the parent. */
export function ShinyFabricBackdrop({
  active = true,
  intensity = 1,
}: {
  /** When false (e.g. empty DONE column), skip animating + paint a
   *  static frame so we don't burn GPU cycles on a hidden surface. */
  active?: boolean
  /** 0..1. Scales the overall brightness/contrast — useful if the
   *  fabric ever ends up reading too loud over a particular card
   *  density. */
  intensity?: number
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const rafRef = useRef<number | null>(null)
  const startTimeRef = useRef<number>(0)
  const isVisibleRef = useRef<boolean>(true)
  const reducedMotionRef = useRef<boolean>(false)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const gl = canvas.getContext('webgl', { antialias: false, premultipliedAlpha: true })
    if (!gl) return

    // Vertex pass-through. We render one fullscreen triangle so there's
    // exactly one vertex shader invocation per frame; the fragment
    // shader does all the work.
    const vertSrc = `
      attribute vec2 a_pos;
      void main() { gl_Position = vec4(a_pos, 0.0, 1.0); }
    `
    const fragSrc = `
      precision highp float;
      uniform vec2 u_res;
      uniform float u_time;
      uniform float u_intensity;

      // 4x4 Bayer ordered dither. Looking up via a precomputed array
      // keeps the threshold deterministic at every pixel — the
      // animation comes purely from the wave field beneath it, so the
      // dither pattern reads as a fixed texture the light moves over.
      float bayer(vec2 p) {
        int x = int(mod(p.x, 4.0));
        int y = int(mod(p.y, 4.0));
        int idx = x + y * 4;
        float v = 0.0;
        if (idx ==  0) v =  0.0;
        if (idx ==  1) v =  8.0;
        if (idx ==  2) v =  2.0;
        if (idx ==  3) v = 10.0;
        if (idx ==  4) v = 12.0;
        if (idx ==  5) v =  4.0;
        if (idx ==  6) v = 14.0;
        if (idx ==  7) v =  6.0;
        if (idx ==  8) v =  3.0;
        if (idx ==  9) v = 11.0;
        if (idx == 10) v =  1.0;
        if (idx == 11) v =  9.0;
        if (idx == 12) v = 15.0;
        if (idx == 13) v =  7.0;
        if (idx == 14) v = 13.0;
        if (idx == 15) v =  5.0;
        return v / 16.0;
      }

      void main() {
        vec2 uv = gl_FragCoord.xy / u_res;
        // Aspect-correct the field so the waves don't look stretched
        // when the column is narrow + tall.
        vec2 p = uv * vec2(u_res.x / max(u_res.y, 1.0), 1.0);

        // Three overlapping sine fields at different frequencies +
        // travel directions. The mix reads as flowing satin — each
        // wave moves in its own direction so the highlights drift
        // rather than pulse uniformly.
        float w1 = sin(p.x * 8.0 + u_time * 0.45) * 0.5;
        float w2 = sin(p.y * 5.0 - u_time * 0.30) * 0.4;
        float w3 = sin((p.x + p.y) * 3.5 + u_time * 0.20) * 0.3;
        float wave = (w1 + w2 + w3) * 0.5 + 0.5;

        // Dither: above the threshold lights up, below stays dark.
        // Multiply both the "lit" and "field" branches by intensity
        // so callers can tune the whole canvas down to a near-static
        // shimmer if needed.
        float d = bayer(gl_FragCoord.xy);
        float lit = step(d, wave * 0.75 + 0.05);
        float brightness = (lit * 0.45 + wave * 0.18) * u_intensity;

        // Cool steel-blue accent (Freyja's signature) tinted toward
        // a warmer green at high points so the highlights feel
        // organic instead of flat one-color.
        vec3 base = vec3(0.05, 0.07, 0.10);
        vec3 hi = mix(vec3(0.40, 0.55, 0.75), vec3(0.55, 0.70, 0.65), wave);
        vec3 col = base + hi * brightness;

        // Slight vignette so the edges of the canvas don't crisp
        // against the surrounding column.
        float vig = smoothstep(0.0, 0.4, min(uv.x, 1.0 - uv.x))
                  * smoothstep(0.0, 0.4, min(uv.y, 1.0 - uv.y));
        col *= 0.7 + 0.3 * vig;

        gl_FragColor = vec4(col, 0.42 * vig + 0.18);
      }
    `

    function compile(type: number, src: string): WebGLShader | null {
      const s = gl!.createShader(type)
      if (!s) return null
      gl!.shaderSource(s, src)
      gl!.compileShader(s)
      if (!gl!.getShaderParameter(s, gl!.COMPILE_STATUS)) {
        // Silently swallow — fabric is decorative, parent UI works
        // without it.
        gl!.deleteShader(s)
        return null
      }
      return s
    }

    const vs = compile(gl.VERTEX_SHADER, vertSrc)
    const fs = compile(gl.FRAGMENT_SHADER, fragSrc)
    if (!vs || !fs) return
    const prog = gl.createProgram()!
    gl.attachShader(prog, vs)
    gl.attachShader(prog, fs)
    gl.linkProgram(prog)
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) return
    gl.useProgram(prog)

    // Fullscreen triangle covers the viewport with three vertices.
    const buf = gl.createBuffer()!
    gl.bindBuffer(gl.ARRAY_BUFFER, buf)
    gl.bufferData(
      gl.ARRAY_BUFFER,
      new Float32Array([-1, -1, 3, -1, -1, 3]),
      gl.STATIC_DRAW,
    )
    const aPos = gl.getAttribLocation(prog, 'a_pos')
    gl.enableVertexAttribArray(aPos)
    gl.vertexAttribPointer(aPos, 2, gl.FLOAT, false, 0, 0)
    const uRes = gl.getUniformLocation(prog, 'u_res')
    const uTime = gl.getUniformLocation(prog, 'u_time')
    const uIntensity = gl.getUniformLocation(prog, 'u_intensity')

    gl.enable(gl.BLEND)
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA)

    const resize = () => {
      const rect = canvas.getBoundingClientRect()
      const dpr = Math.min(window.devicePixelRatio || 1, 2)
      const w = Math.max(1, Math.floor(rect.width * dpr))
      const h = Math.max(1, Math.floor(rect.height * dpr))
      if (canvas.width !== w) canvas.width = w
      if (canvas.height !== h) canvas.height = h
      gl!.viewport(0, 0, w, h)
    }
    resize()
    const ro = new ResizeObserver(resize)
    ro.observe(canvas)

    const motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)')
    reducedMotionRef.current = motionQuery.matches
    const motionListener = () => {
      reducedMotionRef.current = motionQuery.matches
    }
    motionQuery.addEventListener('change', motionListener)

    const io = new IntersectionObserver((entries) => {
      for (const entry of entries) isVisibleRef.current = entry.isIntersecting
    })
    io.observe(canvas)

    startTimeRef.current = performance.now()

    const draw = () => {
      const now = performance.now()
      const t = (now - startTimeRef.current) / 1000
      gl!.uniform2f(uRes, canvas.width, canvas.height)
      gl!.uniform1f(uTime, reducedMotionRef.current ? 0 : t)
      gl!.uniform1f(uIntensity, intensity)
      gl!.drawArrays(gl!.TRIANGLES, 0, 3)
    }

    const loop = () => {
      if (!active || !isVisibleRef.current) {
        rafRef.current = requestAnimationFrame(loop)
        return
      }
      draw()
      rafRef.current = requestAnimationFrame(loop)
    }
    // Initial paint regardless of motion preference / visibility so
    // there's a baseline texture before the loop kicks in.
    draw()
    rafRef.current = requestAnimationFrame(loop)

    return () => {
      if (rafRef.current != null) cancelAnimationFrame(rafRef.current)
      ro.disconnect()
      io.disconnect()
      motionQuery.removeEventListener('change', motionListener)
      gl!.deleteProgram(prog)
      gl!.deleteShader(vs)
      gl!.deleteShader(fs)
      gl!.deleteBuffer(buf)
    }
  }, [active, intensity])

  return (
    <canvas
      ref={canvasRef}
      className="pointer-events-none absolute inset-0 h-full w-full"
      aria-hidden
    />
  )
}
