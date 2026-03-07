import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { InvestmentPromotionComponent } from './investment-promotion.component';

describe('InvestmentPromotionComponent', () => {
  let component: InvestmentPromotionComponent;
  let fixture: ComponentFixture<InvestmentPromotionComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [InvestmentPromotionComponent, NoopAnimationsModule],
    }).compileComponents();

    fixture = TestBed.createComponent(InvestmentPromotionComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
