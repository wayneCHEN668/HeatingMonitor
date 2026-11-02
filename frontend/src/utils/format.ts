export function fmt(v: number | null | undefined, digits = 1, fallback = '--'): string {
  if (v === null || v === undefined || Number.isNaN(v)) return fallback
  return Number(v).toFixed(digits)
}

export function fmtInt(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '--'
  return Math.round(v).toLocaleString('zh-CN')
}

export function pct(v: number | null | undefined, digits = 1, sign = false): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '--'
  const s = Number(v).toFixed(digits)
  return `${sign && v > 0 ? '+' : ''}${s}%`
}

export const MODE_TEXT: Record<string, string> = { curve: '气候补偿', fixed: '定供温', manual: '手动阀位' }
export const KIND_TEXT: Record<string, string> = { chp: '热电联产', gas: '燃气锅炉', heatpump: '热泵+电蓄热' }
export const LEVEL_TEXT: Record<number, string> = { 1: '一级', 2: '二级', 3: '三级' }
export const LEVEL_TYPE: Record<number, 'danger' | 'warning' | 'info'> = { 1: 'danger', 2: 'warning', 3: 'info' }

export function todayStr(offset = 0) {
  const d = new Date(Date.now() + offset * 86400000)
  const z = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${z(d.getMonth() + 1)}-${z(d.getDate())}`
}

/** 温度 -> 颜色（供水暖色、回水冷色的连续映射，用于工艺图管道着色） */
export function tempColor(t: number, lo = 20, hi = 110): string {
  const x = Math.max(0, Math.min(1, (t - lo) / (hi - lo)))
  // 蓝(低) -> 青 -> 橙 -> 红(高)
  const stops = [
    [0, [59, 130, 246]],
    [0.35, [34, 211, 238]],
    [0.65, [251, 146, 60]],
    [1, [239, 68, 68]],
  ] as const
  for (let i = 1; i < stops.length; i++) {
    if (x <= stops[i][0]) {
      const [x0, c0] = stops[i - 1]
      const [x1, c1] = stops[i]
      const k = (x - x0) / (x1 - x0)
      const c = c0.map((v, j) => Math.round(v + (c1[j] - v) * k))
      return `rgb(${c[0]},${c[1]},${c[2]})`
    }
  }
  return 'rgb(239,68,68)'
}

/** 管道着色：供水暖色系（琥珀→红）、回水冷色系（青→靛），亮度随温度变化，保证供/回一眼可辨 */
export function pipeColor(kind: 'supply' | 'return', t: number, lo: number, hi: number): string {
  const x = Math.max(0, Math.min(1, (t - lo) / (hi - lo)))
  const [a, b] = kind === 'supply' ? [[251, 191, 36], [239, 68, 68]] : [[34, 211, 238], [99, 102, 241]]
  const c = a.map((v, i) => Math.round(v + (b[i] - v) * x))
  return `rgb(${c[0]},${c[1]},${c[2]})`
}
