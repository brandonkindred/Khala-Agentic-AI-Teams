import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { InvestmentStrategyComponent } from './investment-strategy.component';

describe('InvestmentStrategyComponent', () => {
  let component: InvestmentStrategyComponent;
  let fixture: ComponentFixture<InvestmentStrategyComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [InvestmentStrategyComponent, NoopAnimationsModule],
    }).compileComponents();

    fixture = TestBed.createComponent(InvestmentStrategyComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
