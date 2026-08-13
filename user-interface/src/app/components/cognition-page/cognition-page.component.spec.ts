import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { of } from 'rxjs';
import { vi } from 'vitest';
import { CognitionPageComponent } from './cognition-page.component';
import { AgentCatalogApiService } from '../../services/agent-catalog-api.service';
import { CognitionApiService } from '../../services/cognition-api.service';

/**
 * The host mounts CognitionTabComponent, which calls listAgents() on init.
 * Stub both services so TestBed can construct the tab.
 */
function catalogSpy() {
  return { listAgents: vi.fn().mockReturnValue(of([])) };
}

function cognitionApiSpy() {
  return {
    listProposals: vi.fn().mockReturnValue(of([])),
    approveProposal: vi.fn(),
    rejectProposal: vi.fn(),
    listMemoryEvents: vi.fn().mockReturnValue(of([])),
    listRules: vi.fn().mockReturnValue(of([])),
  };
}

describe('CognitionPageComponent', () => {
  let fixture: ComponentFixture<CognitionPageComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [CognitionPageComponent, NoopAnimationsModule],
      providers: [
        { provide: AgentCatalogApiService, useValue: catalogSpy() },
        { provide: CognitionApiService, useValue: cognitionApiSpy() },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(CognitionPageComponent);
    fixture.detectChanges();
  });

  it('creates', () => {
    expect(fixture.componentInstance).toBeTruthy();
  });

  it('mounts the Cognition tab', () => {
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('app-cognition-tab')).toBeTruthy();
  });
});
