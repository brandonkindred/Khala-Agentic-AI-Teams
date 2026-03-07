import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { MarketResearchFormComponent } from './market-research-form.component';

describe('MarketResearchFormComponent', () => {
  let component: MarketResearchFormComponent;
  let fixture: ComponentFixture<MarketResearchFormComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [MarketResearchFormComponent, NoopAnimationsModule],
    }).compileComponents();

    fixture = TestBed.createComponent(MarketResearchFormComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
