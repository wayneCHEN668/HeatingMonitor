/** 安全的文本渲染工具：不使用 v-html，上传文档中的任何标记都只会按纯文本显示 */

export interface Seg {
  text: string
  hl: boolean
}

/** 把文本按高亮词切分成片段（最长词优先） */
export function highlight(text: string, terms: string[]): Seg[] {
  const ts = [...new Set(terms.filter((t) => t && t.length > 1))].sort((a, b) => b.length - a.length)
  if (!ts.length) return [{ text, hl: false }]
  const lower = text.toLowerCase()
  const out: Seg[] = []
  let i = 0
  while (i < text.length) {
    let hit = ''
    for (const t of ts) if (lower.startsWith(t.toLowerCase(), i)) { hit = text.slice(i, i + t.length); break }
    if (hit) {
      out.push({ text: hit, hl: true })
      i += hit.length
    } else {
      const last = out[out.length - 1]
      if (last && !last.hl) last.text += text[i]
      else out.push({ text: text[i], hl: false })
      i++
    }
  }
  return out
}

export interface Block {
  kind: 'h1' | 'h2' | 'h3' | 'li' | 'oli' | 'p'
  text: string
}

/** 极简 Markdown → 块（标题、列表、段落），用于文档阅读 */
export function mdBlocks(md: string): Block[] {
  const blocks: Block[] = []
  let para: string[] = []
  const flush = () => {
    if (para.length) blocks.push({ kind: 'p', text: para.join('') })
    para = []
  }
  for (const raw of md.split('\n')) {
    const line = raw.trimEnd()
    let m
    if (!line.trim()) flush()
    else if ((m = line.match(/^(#{1,3})\s+(.*)$/))) {
      flush()
      blocks.push({ kind: (['h1', 'h2', 'h3'] as const)[m[1].length - 1], text: m[2] })
    } else if ((m = line.match(/^\s*[-*]\s+(.*)$/))) {
      flush()
      blocks.push({ kind: 'li', text: m[1] })
    } else if ((m = line.match(/^\s*(\d+\.\s+.*)$/))) {
      flush()
      blocks.push({ kind: 'oli', text: m[1] })
    } else if (line.startsWith('>')) {
      flush()
      blocks.push({ kind: 'p', text: line.replace(/^>\s*/, '') })
    } else para.push(line.trim())
  }
  flush()
  return blocks
}
