import { TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { MatDialogRef, MAT_DIALOG_DATA } from '@angular/material/dialog';
import {
  CodeReviewSystemicFindingsDialogComponent,
  type CodeReviewSystemicFindingsDialogData,
} from './code-review-systemic-findings-dialog.component';
import type { SystemicFinding } from '../../../models/coding-team.model';

describe('CodeReviewSystemicFindingsDialogComponent', () => {
  let dialogRef: { close: ReturnType<typeof vi.fn> };

  const findings: SystemicFinding[] = [
    {
      title: 'Missing validation repeated',
      description: 'Three call sites skip input validation.',
      related_locations: [
        { file_path: 'a.py', description: 'missing check in f' },
        { file_path: 'b.py', description: 'missing check in g' },
      ],
    },
  ];

  function build(data: CodeReviewSystemicFindingsDialogData) {
    TestBed.configureTestingModule({
      imports: [CodeReviewSystemicFindingsDialogComponent, NoopAnimationsModule],
      providers: [
        { provide: MatDialogRef, useValue: dialogRef },
        { provide: MAT_DIALOG_DATA, useValue: data },
      ],
    });
    return TestBed.createComponent(CodeReviewSystemicFindingsDialogComponent);
  }

  beforeEach(() => {
    dialogRef = { close: vi.fn() };
  });

  it('renders each finding (title, description, related locations)', () => {
    const fixture = build({ findings });
    fixture.detectChanges();
    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('Missing validation repeated');
    expect(text).toContain('Three call sites skip input validation.');
    expect(text).toContain('a.py');
    expect(text).toContain('missing check in f');
    expect(text).toContain('b.py');
  });

  it('renders an empty-state message when there are no findings', () => {
    const fixture = build({ findings: [] });
    fixture.detectChanges();
    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('No cross-cutting patterns were found in this review.');
  });

  it('closes the dialog via the ref', () => {
    const fixture = build({ findings });
    fixture.detectChanges();
    fixture.componentInstance.close();
    expect(dialogRef.close).toHaveBeenCalled();
  });
});
