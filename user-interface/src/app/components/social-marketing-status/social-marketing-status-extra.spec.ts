import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi, beforeEach, afterEach } from 'vitest';
import { SocialMarketingApiService } from '../../services/social-marketing-api.service';
import { SocialMarketingStatusComponent } from './social-marketing-status.component';

describe('SocialMarketingStatusComponent (extra coverage)', () => {
  let api: { getStatus: ReturnType<typeof vi.fn> };
  let component: SocialMarketingStatusComponent;
  let fixture: ComponentFixture<SocialMarketingStatusComponent>;

  beforeEach(async () => {
    api = { getStatus: vi.fn().mockReturnValue(of({ job_id: 'j1', status: 'running' })) };
    await TestBed.configureTestingModule({
      imports: [SocialMarketingStatusComponent, NoopAnimationsModule],
      providers: [{ provide: SocialMarketingApiService, useValue: api }],
    }).compileComponents();
    fixture = TestBed.createComponent(SocialMarketingStatusComponent);
    component = fixture.componentInstance;
  });

  afterEach(() => {
    TestBed.resetTestingModule();
    vi.useRealTimers();
  });

  it('does not poll when jobId is null', () => {
    component.jobId = null;
    fixture.detectChanges();
    expect(component.loading).toBe(false);
    expect(api.getStatus).not.toHaveBeenCalled();
  });

  it('polls and updates status', async () => {
    vi.useFakeTimers();
    api.getStatus.mockReturnValue(of({ job_id: 'j1', status: 'running' }));
    component.jobId = 'j1';
    fixture.detectChanges();
    await vi.advanceTimersByTimeAsync(1);
    expect(api.getStatus).toHaveBeenCalledWith('j1');
    expect(component.status?.status).toBe('running');
    expect(component.loading).toBe(false);
  });

  it('stops polling on completed status', async () => {
    vi.useFakeTimers();
    api.getStatus.mockReturnValue(of({ job_id: 'j1', status: 'completed' }));
    component.jobId = 'j1';
    fixture.detectChanges();
    await vi.advanceTimersByTimeAsync(1);
    expect(component['sub']).toBeNull();
  });

  it('stops polling on failed status', async () => {
    vi.useFakeTimers();
    api.getStatus.mockReturnValue(of({ job_id: 'j1', status: 'failed' }));
    component.jobId = 'j1';
    fixture.detectChanges();
    await vi.advanceTimersByTimeAsync(1);
    expect(component['sub']).toBeNull();
  });

  it('stops polling on cancelled status', async () => {
    vi.useFakeTimers();
    api.getStatus.mockReturnValue(of({ job_id: 'j1', status: 'cancelled' }));
    component.jobId = 'j1';
    fixture.detectChanges();
    await vi.advanceTimersByTimeAsync(1);
    expect(component['sub']).toBeNull();
  });

  it('handles polling error', async () => {
    vi.useFakeTimers();
    api.getStatus.mockReturnValue(throwError(() => new Error('x')));
    component.jobId = 'j1';
    fixture.detectChanges();
    await vi.advanceTimersByTimeAsync(1);
    expect(component.loading).toBe(false);
    expect(component['sub']).toBeNull();
  });

  it('ngOnDestroy unsubscribes', async () => {
    vi.useFakeTimers();
    api.getStatus.mockReturnValue(of({ job_id: 'j1', status: 'running' }));
    component.jobId = 'j1';
    fixture.detectChanges();
    await vi.advanceTimersByTimeAsync(1);
    const sub = component['sub'];
    const spy = vi.spyOn(sub!, 'unsubscribe');
    component.ngOnDestroy();
    expect(spy).toHaveBeenCalled();
  });
});
