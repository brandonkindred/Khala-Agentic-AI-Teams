import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { InvestmentApiService } from '../../services/investment-api.service';
import { InvestmentWorkflowComponent } from './investment-workflow.component';

describe('InvestmentWorkflowComponent', () => {
  let component: InvestmentWorkflowComponent;
  let fixture: ComponentFixture<InvestmentWorkflowComponent>;

  beforeEach(async () => {
    const apiSpy = { getWorkflowStatus: vi.fn().mockReturnValue(of({ mode: 'idle', audit_log: [], queue_counts: {} })) };
    await TestBed.configureTestingModule({
      imports: [InvestmentWorkflowComponent, NoopAnimationsModule],
      providers: [{ provide: InvestmentApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(InvestmentWorkflowComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
