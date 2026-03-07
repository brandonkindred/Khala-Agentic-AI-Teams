import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { InvestmentProfileFormComponent } from './investment-profile-form.component';

describe('InvestmentProfileFormComponent', () => {
  let component: InvestmentProfileFormComponent;
  let fixture: ComponentFixture<InvestmentProfileFormComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [InvestmentProfileFormComponent, NoopAnimationsModule],
    }).compileComponents();

    fixture = TestBed.createComponent(InvestmentProfileFormComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
