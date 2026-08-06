import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

// Lightweight source-contract test: no DOM mount harness is configured in this
// project, so verify the return-navigation contract directly against the
// App.vue template and scoped styles.
const source = readFileSync(fileURLToPath(new URL('./App.vue', import.meta.url)), 'utf8')

describe('App.vue return navigation', () => {
  it('renders a link back to /panel inside the authenticated main view only', () => {
    expect(source).toMatch(/href="\/panel"/)
    expect(source).toContain('class="panel-back-btn"')

    const mainLayout = source.indexOf('v-else class="app-layout"')
    const loginBlock = source.indexOf('v-else-if="!authenticated"')
    const backBtn = source.indexOf('panel-back-btn')

    expect(mainLayout).toBeGreaterThan(-1)
    expect(backBtn).toBeGreaterThan(mainLayout)
    // The loading and login templates must not contain the back button.
    const preMain = source.slice(0, mainLayout)
    expect(preMain).not.toContain('panel-back-btn')
    expect(loginBlock).toBeGreaterThan(-1)
  })

  it('does not open a new tab or window', () => {
    expect(source).not.toContain('target="_blank"')
  })

  it('does not rely on history.back()', () => {
    expect(source).not.toMatch(/history\.back\s*\(/)
  })

  it('exposes an accessible name and visible label', () => {
    expect(source).toContain('aria-label="返回控制面板"')
    expect(source).toContain('title="返回控制面板"')
    expect(source).toContain('<span aria-hidden="true">←</span>')
    expect(source).toContain('<span class="panel-back-text">返回控制面板</span>')
  })

  it('keeps the arrow entry on narrow screens', () => {
    const mediaStart = source.indexOf('@media (max-width: 768px)')
    expect(mediaStart).toBeGreaterThan(-1)
    const media = source.slice(mediaStart)
    // Hide the label text but never the link itself.
    expect(media).toMatch(/\.panel-back-text\s*\{\s*display:\s*none\s*;/)
    expect(media).not.toMatch(/\.panel-back-btn\s*\{\s*display:\s*none\s*;/)
  })
})
