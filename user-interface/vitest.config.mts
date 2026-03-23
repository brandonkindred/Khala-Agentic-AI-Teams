import { defineConfig } from 'vitest/config';
import angular from '@analogjs/vite-plugin-angular';

export default defineConfig({
  plugins: [angular({ tsconfig: 'tsconfig.spec.json', jit: true })],
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['src/test-setup.mjs'],
    include: ['src/**/*.spec.ts'],
    coverage: {
      provider: 'v8',
      include: ['src/app/**/*.ts'],
      exclude: ['**/*.spec.ts', '**/*.model.ts', '**/environments/*.ts', '**/index.ts', '**/*.module.ts'],
      thresholds: {
        lines: 60,
      },
    },
  },
});
