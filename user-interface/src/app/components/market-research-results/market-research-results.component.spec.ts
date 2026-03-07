import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { MarketResearchResultsComponent } from './market-research-results.component';

describe('MarketResearchResultsComponent', () => {
  let component: MarketResearchResultsComponent;
  let fixture: ComponentFixture<MarketResearchResultsComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [MarketResearchResultsComponent, NoopAnimationsModule],
    }).compileComponents();

    fixture = TestBed.createComponent(MarketResearchResultsComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
