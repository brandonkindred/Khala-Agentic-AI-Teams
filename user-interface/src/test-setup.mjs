import 'zone.js';
import 'zone.js/plugins/sync-test';
import 'zone.js/plugins/proxy';
import 'zone.js/testing';
import { getTestBed } from '@angular/core/testing';
import { BrowserDynamicTestingModule, platformBrowserDynamicTesting } from '@angular/platform-browser-dynamic/testing';
import { expect } from 'vitest';
import * as axeMatchers from 'vitest-axe/matchers';

console.log('[SETUP.MJS RUNNING]');
getTestBed().initTestEnvironment(BrowserDynamicTestingModule, platformBrowserDynamicTesting());
expect.extend(axeMatchers);
console.log('[SETUP.MJS DONE]');
