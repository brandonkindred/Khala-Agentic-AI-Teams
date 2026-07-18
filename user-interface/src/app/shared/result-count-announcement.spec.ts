import { resultCountAnnouncement } from './result-count-announcement';

describe('resultCountAnnouncement', () => {
  it('uses the singular form for exactly one', () => {
    expect(resultCountAnnouncement(1, 'repository', 'repositories')).toBe('1 repository shown');
  });

  it('uses the plural form for zero and for more than one', () => {
    expect(resultCountAnnouncement(0, 'repository', 'repositories')).toBe('0 repositories shown');
    expect(resultCountAnnouncement(3, 'repository', 'repositories')).toBe('3 repositories shown');
  });
});
