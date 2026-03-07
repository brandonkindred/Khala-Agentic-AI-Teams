import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { vi } from 'vitest';
import { MarketResearchApiService } from '../../services/market-research-api.service';
import { MarketResearchDashboardComponent } from './market-research-dashboard.component';

describe('MarketResearchDashboardComponent', () => {
  let component: MarketResearchDashboardComponent;
  let fixture: ComponentFixture<MarketResearchDashboardComponent>;
  let apiSpy: { run: ReturnType<typeof vi.fn>; health: ReturnType<typeof vi.fn> };

  beforeEach(async () => {
    apiSpy = {
      run: vi.fn(),
      health: vi.fn().mockReturnValue(of({ status: 'ok' })),
    };
    await TestBed.configureTestingModule({
      imports: [MarketResearchDashboardComponent],
      providers: [{ provide: MarketResearchApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(MarketResearchDashboardComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('onSubmit should call api.run and set result on success', () => {
    const mockResult = { output: {}, artifacts: [] } as any;
    apiSpy.run.mockReturnValue(of(mockResult));
    component.onSubmit({ query: 'test query' } as any);
    expect(apiSpy.run).toHaveBeenCalledWith({ query: 'test query' });
    expect(component.result).toEqual(mockResult);
    expect(component.loading).toBe(false);
  });

  it('onSubmit should set error on failure', () => {
    apiSpy.run.mockReturnValue(throwError(() => ({ error: { detail: 'Failed' } })));
    component.onSubmit({ query: 'x' } as any);
    expect(component.error).toBeTruthy();
    expect(component.loading).toBe(false);
  });

  it('healthCheck should call api.health', () => {
    component.healthCheck().subscribe((r) => expect(r).toEqual({ status: 'ok' }));
    expect(apiSpy.health).toHaveBeenCalled();
  });
});
