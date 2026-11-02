/** 后端统一返回封套（与 AI 工具设计文档第十节一致） */
export interface Envelope<T> {
  ok: boolean
  summary: string
  data: T
  evidence: string[]
  confidence: number | null
  warnings: string[]
  error?: { code: string; message: string; retryable: boolean }
  trace_id: string
}

export type CurvePoint = [number, number]

export interface SourceMeta {
  id: string
  name: string
  kind: 'chp' | 'gas' | 'heatpump'
  capacity_mw: number
  xy: [number, number]
  units: { id: string; name: string; type: string; cap_mw?: number }[]
  curve: CurvePoint[]
  stations: string[]
}

export interface StationMeta {
  id: string
  name: string
  source_id: string
  area: number
  system: 'radiator' | 'floor'
  design_index: number
  xy: [number, number]
  autonomy: string
  address: string
  pump_kw: number
}

export interface Meta {
  sources: SourceMeta[]
  stations: StationMeta[]
  metrics: Record<string, { name: string; unit: string }>
  curve_tout: number[]
  limits: Record<string, any>
}

export interface StationRT {
  id: string
  name: string
  source_id: string
  online: boolean
  mode: 'curve' | 'fixed' | 'manual'
  tout: number
  ts1: number
  tr1: number
  p1s: number
  p1r: number
  g1: number
  ts2: number
  tr2: number
  p2s: number
  p2r: number
  g2: number
  ts2_set: number
  valve: number
  valve_set: number
  tin: number
  heat_mw: number
  q1_mw: number
  q2_mw: number
  pump_freq: number
  pump_kw: number
  makeup_tph: number
  tank_level: number
  valve_sat: boolean
  autonomy: string
  system: string
  area: number
}

export interface UnitRT {
  id: string
  name: string
  type: string
  running: boolean
  load?: number
  outlet?: number
  freq?: number
  power_kw?: number
  steam_tph?: number
  gas_nm3h?: number
  flue_t?: number
  eff?: number
  cop?: number
  soc?: number
  mode?: string
  top_t?: number
  bottom_t?: number
  cap_mw?: number
}

export interface SourceRT {
  id: string
  name: string
  kind: 'chp' | 'gas' | 'heatpump'
  tout: number
  ts1: number
  tr1: number
  g: number
  p_s: number
  p_r: number
  heat_mw: number
  capacity_mw: number
  load_pct: number
  pump_kw: number
  makeup_tph: number
  units: UnitRT[]
  steam_tph?: number
  steam_p?: number
  steam_t?: number
  condensate_t?: number
  gas_nm3h?: number
  eff?: number
  hp_mw?: number
  eb_mw?: number
  tank_dis_mw?: number
  tank_soc?: number
  cop?: number
  power_kw?: number
}

export interface Alarm {
  id: string
  object_id: string
  object_name: string
  level: 1 | 2 | 3
  code: string
  message: string
  value: number | null
  first_ts: string
  last_ts: string
  acked: boolean
  active: boolean
  cleared_ts?: string
}

export interface Snapshot {
  ts: string
  tout: number
  stations: Record<string, StationRT>
  sources: Record<string, SourceRT>
  network: {
    heat_mw: number
    capacity_mw: number
    flow_tph: number
    tin_avg: number
    online: number
    total: number
    area: number
    elec_price: number
  }
  alarms: Alarm[]
  today: {
    network: Record<string, number>
    objects: Record<string, Record<string, number>>
  }
}

export interface ControlSettings {
  mode: 'curve' | 'fixed' | 'manual'
  curve: CurvePoint[]
  ts2_fixed: number
  valve_set: number
  freq: number
}

export interface CheckItem {
  name: string
  status: 'pass' | 'warn' | 'block'
  message: string
}

export interface Draft {
  draft_id: string
  station_id: string
  station_name: string
  param: string
  param_text: string
  value: any
  description: string
  reason: string
  source: string
  created_by: string
  created_at: string
  status: 'drafted' | 'checked' | 'blocked' | 'approved' | 'executed' | 'rejected'
  expected: { before: Prediction; after: Prediction }
  check: { verdict: 'pass' | 'warn' | 'block'; items: CheckItem[]; ts: string } | null
  approval: { approver: string; comment: string; token: string; expires_at: string; ts: string } | null
  command_id?: string
}

export interface Prediction {
  ts2_set: number | null
  valve: number
  saturated: boolean
  g1: number
  g2: number
  heat_mw: number
  ts2: number
  tr2: number
  tr1: number
  tin: number
}

export interface Command {
  command_id: string
  draft_id: string
  station_id: string
  station_name: string
  param: string
  param_text: string
  value: any
  description: string
  executed_by: string
  approver: string
  executed_at: string
  status: 'executed' | 'rolled_back'
  source: string
  ack: { status: string; latency_ms: number }
  expected: Prediction
  rollback?: { by: string; reason: string; ts: string }
}

export interface Policy {
  station_id: string
  autonomy: string
  auto_sign: boolean
  params: Record<string, any>
  max_cmds_per_hour: number
  interlocks: string[]
  approvers: string[]
}

export interface CommandRequest {
  station_id: string
  param: 'valve_opening' | 'control_mode' | 'supply_temp_setpoint' | 'climate_curve' | 'pump_frequency'
  value: any
  reason?: string
  source?: 'manual' | 'ai'
}
