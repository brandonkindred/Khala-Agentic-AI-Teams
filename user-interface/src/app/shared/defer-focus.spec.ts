import { deferFocus } from './defer-focus';

describe('deferFocus', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
    document.body.innerHTML = '';
  });

  function host(html: string): HTMLElement {
    const el = document.createElement('div');
    el.innerHTML = html;
    document.body.appendChild(el);
    return el;
  }

  it('focuses the resolved element after the tick', () => {
    const root = host('<button id="a">A</button>');
    deferFocus(root, (h) => h.querySelector<HTMLElement>('#a'));
    expect(document.activeElement).not.toBe(root.querySelector('#a'));
    vi.runAllTimers();
    expect(document.activeElement).toBe(root.querySelector('#a'));
  });

  it('does nothing when the resolver returns null', () => {
    const root = host('<button id="a">A</button>');
    const before = document.activeElement;
    deferFocus(root, () => null);
    vi.runAllTimers();
    expect(document.activeElement).toBe(before);
  });

  it('returns a handle that can be cleared before it fires', () => {
    const root = host('<button id="a">A</button>');
    const handle = deferFocus(root, (h) => h.querySelector<HTMLElement>('#a'));
    clearTimeout(handle);
    vi.runAllTimers();
    expect(document.activeElement).not.toBe(root.querySelector('#a'));
  });
});
