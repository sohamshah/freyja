import { memo, useMemo } from 'react'
import type { CSSProperties } from 'react'
import { layoutSeam } from '../../lib/seam'
import type { SeamUnit } from '../../lib/seam'

/**
 * Draws a seam (see lib/seam.ts). Every stroke carries pathLength=1 so the
 * fold can draw it in by fraction, and every piece carries its x span so
 * the fold can write the line left to right at one speed.
 *
 * `fresh` draws each new piece in as it mounts: a helper's seam grows this
 * way while the helper works. `hits` adds hover targets, one per unit,
 * that WorkStretch reads for its tooltip and cross-highlight.
 */
export const Seam = memo(function Seam({
  units,
  maxWidth,
  fresh = false,
  hits = false,
  className = '',
}: {
  units: SeamUnit[]
  maxWidth?: number
  fresh?: boolean
  hits?: boolean
  className?: string
}) {
  const key = JSON.stringify(units)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const geom = useMemo(() => layoutSeam(units, maxWidth), [key, maxWidth])
  const w = Math.ceil(geom.width)
  return (
    <svg
      className={`wk-seam-svg ${className}`}
      width={w}
      height={geom.height}
      viewBox={`0 0 ${w} ${geom.height}`}
      data-w={geom.width}
      aria-hidden="true"
    >
      {geom.pieces.map((p, i) => (
        <g
          key={i}
          data-piece=""
          data-x0={p.x0}
          data-x1={p.x1}
          data-u={p.unit >= 0 ? p.unit : undefined}
          className={fresh ? 'fresh' : undefined}
        >
          {p.paths.map((q, j) => {
            const style: Record<string, string> = {}
            if (q.width) style['--sw'] = String(q.width)
            if (q.range) {
              style.animationDelay = `calc(${Math.round(q.range[0] * 620)}ms * var(--seam-t, 1))`
              style.animationDuration = `calc(${Math.round((q.range[1] - q.range[0]) * 620)}ms * var(--seam-t, 1))`
            }
            return (
              <path
                key={q.key ?? j}
                d={q.d}
                className={q.cls.split(' ').map((c) => `s-${c}`).join(' ')}
                pathLength={1}
                data-main={q.main ? '' : undefined}
                data-r={q.range ? q.range.join(',') : undefined}
                style={style as CSSProperties}
              />
            )
          })}
        </g>
      ))}
      {hits &&
        geom.units.map((u, i) => (
          <rect
            key={`h${i}`}
            className="wk-seam-hit"
            data-hit={i}
            data-cx={u.cx}
            data-sy={u.strandY.join(',')}
            data-label={u.label}
            x={u.x0 - 2}
            y={0}
            width={u.x1 - u.x0 + 4}
            height={geom.height}
          />
        ))}
    </svg>
  )
})
