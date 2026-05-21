import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { vi, beforeEach } from 'vitest';
import { provideHttpClient } from '@angular/common/http';
import { MarketResearchApiService } from '../../services/market-research-api.service';
import { MarketResearchDashboardComponent } from './market-research-dashboard.component';

describe('MarketResearchDashboardComponent (extra coverage)', () => {
  let component: MarketResearchDashboardComponent;
  let fixture: ComponentFixture<MarketResearchDashboardComponent>;
  let api: { health: ReturnType<typeof vi.fn> };

  beforeEach(async () => {
    api = { health: vi.fn().mockReturnValue(of({ status: 'ok' })) };
    await TestBed.configureTestingModule({
      imports: [MarketResearchDashboardComponent],
      providers: [provideHttpClient(), { provide: MarketResearchApiService, useValue: api }],
    }).compileComponents();
    fixture = TestBed.createComponent(MarketResearchDashboardComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('onWorkflowLaunched stores result and JSON', () => {
    component.onWorkflowLaunched({
      job_id: 'j1',
      conversation_id: 'c1',
      upstream_status: 200,
      upstream_body: { job_id: 'j1', status: 'queued' },
    });
    expect(component.lastResult).toEqual({ job_id: 'j1', status: 'queued' });
    expect(component.lastResultJson).toContain('job_id');
  });

  it('onWorkflowLaunched handles JSON.stringify error gracefully', () => {
    const circular: Record<string, unknown> = {};
    circular['self'] = circular;
    component.onWorkflowLaunched({
      job_id: null,
      conversation_id: 'c1',
      upstream_status: 200,
      upstream_body: circular,
    });
    expect(component.lastResult).toBe(circular);
    expect(component.lastResultJson.length).toBeGreaterThan(0);
  });

  it('clearResult resets state', () => {
    component.lastResult = { x: 1 };
    component.lastResultJson = '{"x":1}';
    component.clearResult();
    expect(component.lastResult).toBeNull();
    expect(component.lastResultJson).toBe('');
  });
});
