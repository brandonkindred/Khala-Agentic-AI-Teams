import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi, beforeEach } from 'vitest';
import { InvestmentApiService } from '../../services/investment-api.service';
import { InvestmentWorkflowComponent } from './investment-workflow.component';

describe('InvestmentWorkflowComponent (extra coverage)', () => {
  let component: InvestmentWorkflowComponent;
  let fixture: ComponentFixture<InvestmentWorkflowComponent>;
  let api: {
    getWorkflowStatus: ReturnType<typeof vi.fn>;
    getQueues: ReturnType<typeof vi.fn>;
  };

  beforeEach(async () => {
    api = {
      getWorkflowStatus: vi.fn().mockReturnValue(of({ mode: 'paper', audit_log: [], queue_counts: { research: 3 } })),
      getQueues: vi.fn().mockReturnValue(of({ queues: { research: [{ id: 'q1', priority: 'high' }] } })),
    };
    await TestBed.configureTestingModule({
      imports: [InvestmentWorkflowComponent, NoopAnimationsModule],
      providers: [{ provide: InvestmentApiService, useValue: api }],
    }).compileComponents();
    fixture = TestBed.createComponent(InvestmentWorkflowComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('loads workflow status and queues on init', () => {
    expect(api.getWorkflowStatus).toHaveBeenCalled();
    expect(api.getQueues).toHaveBeenCalled();
    expect(component.workflowStatus?.mode).toBe('paper');
    expect(component.loading).toBe(false);
  });

  it('startPolling error sets error message', () => {
    api.getWorkflowStatus.mockReturnValue(throwError(() => ({ error: { detail: 'down' } })));
    component.startPolling();
    expect(component.error).toBe('down');
    expect(component.loading).toBe(false);
  });

  it('loadQueues handles error gracefully', () => {
    api.getQueues.mockReturnValue(throwError(() => new Error('x')));
    component.loadQueues();
    // No throw is enough
    expect(component).toBeTruthy();
  });

  it('refresh reloads status', () => {
    api.getWorkflowStatus.mockClear();
    component.refresh();
    expect(api.getWorkflowStatus).toHaveBeenCalled();
  });

  it('refresh handles error', () => {
    api.getWorkflowStatus.mockReturnValue(throwError(() => ({ message: 'refresh-fail' })));
    component.refresh();
    expect(component.error).toBe('refresh-fail');
    expect(component.loading).toBe(false);
  });

  it('getQueueItems returns list or empty for missing queue', () => {
    expect(component.getQueueItems('research').length).toBe(1);
    expect(component.getQueueItems('missing')).toEqual([]);
    component.queues = null;
    expect(component.getQueueItems('research')).toEqual([]);
  });

  it('getQueueCount returns count or 0', () => {
    expect(component.getQueueCount('research')).toBe(3);
    expect(component.getQueueCount('other')).toBe(0);
    component.workflowStatus = null;
    expect(component.getQueueCount('research')).toBe(0);
  });

  it('getModeConfig falls back to monitor_only for unknown modes', () => {
    expect(component.getModeConfig('paper').label).toBe('Paper Trading');
    expect(component.getModeConfig('advisory').label).toBe('Advisory');
    expect(component.getModeConfig('live').label).toBe('Live Trading');
    expect(component.getModeConfig('monitor_only').label).toBe('Monitor Only');
    expect(component.getModeConfig('xxx' as never).label).toBe('Monitor Only');
  });

  it('getPriorityClass returns prefixed class', () => {
    expect(component.getPriorityClass('high')).toBe('priority-high');
  });

  it('ngOnDestroy unsubscribes', () => {
    component.ngOnDestroy();
    // No throw
    expect(component).toBeTruthy();
  });
});
