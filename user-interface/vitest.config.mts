import { defineConfig } from 'vitest/config';
import angular from '@analogjs/vite-plugin-angular';

export default defineConfig({
  plugins: [angular({ tsconfig: 'tsconfig.spec.json', jit: true })],
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['src/test-setup.mjs'],
    include: ['src/**/*.spec.ts'],
    pool: 'forks',
    poolOptions: {
      forks: {
        execArgv: ['--max-old-space-size=4096'],
      },
    },
    coverage: {
      provider: 'v8',
      include: ['src/app/**/*.ts'],
      exclude: ['**/*.spec.ts', '**/testing/**', '**/*.mock.ts', '**/*.model.ts', '**/environments/*.ts', '**/index.ts', '**/*.module.ts'],
      thresholds: {
        lines: 90,
      },
    },
  },
});
