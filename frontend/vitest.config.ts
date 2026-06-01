import { defineConfig } from 'vitest/config'

// Pure-logic unit tests run in a node environment (no DOM); the UI is verified by
// the manual Phase 1 DoD. Kept separate from vite.config.ts so the React plugin's
// types don't clash with vitest's bundled vite.
export default defineConfig({
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
})
