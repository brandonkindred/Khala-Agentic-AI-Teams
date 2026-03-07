import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { vi } from 'vitest';
import { BloggingApiService } from '../../services/blogging-api.service';
import { SoftwareEngineeringApiService } from '../../services/software-engineering-api.service';
import { MarketResearchApiService } from '../../services/market-research-api.service';
import { Soc2ComplianceApiService } from '../../services/soc2-compliance-api.service';
import { SocialMarketingApiService } from '../../services/social-marketing-api.service';
import { BrandingApiService } from '../../services/branding-api.service';
import { ApiStatusWidgetComponent } from './api-status-widget.component';

describe('ApiStatusWidgetComponent', () => {
  let component: ApiStatusWidgetComponent;
  let fixture: ComponentFixture<ApiStatusWidgetComponent>;

  const health = () => of({ status: 'ok' });

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [ApiStatusWidgetComponent],
      providers: [
        { provide: BloggingApiService, useValue: { health } },
        { provide: SoftwareEngineeringApiService, useValue: { health } },
        { provide: MarketResearchApiService, useValue: { health } },
        { provide: Soc2ComplianceApiService, useValue: { health: vi.fn().mockReturnValue(of({ status: 'ok' })) } },
        { provide: SocialMarketingApiService, useValue: { health } },
        { provide: BrandingApiService, useValue: { health } },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(ApiStatusWidgetComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should load statuses on init', () => {
    expect(component.statuses.length).toBeGreaterThan(0);
    expect(component.loading).toBe(false);
  });
});
