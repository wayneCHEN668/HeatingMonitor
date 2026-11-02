/** 工艺图通用小部件的几何辅助 */
export interface TagItem {
  x: number
  y: number
  label: string
  value: string
  unit?: string
  color?: string
  w?: number
}

export const fixed = (v: number | undefined | null, d = 1) =>
  v === undefined || v === null || Number.isNaN(v) ? '--' : Number(v).toFixed(d)
