import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { SocialMarketingApiService } from '../../services/social-marketing-api.service';
import { SocialMarketingStatusComponent } from './social-marketing-status.component';

describe('SocialMarketingStatusComponent', () => {
  let component: SocialMarketingStatusComponent;
  let fixture: ComponentFixture<SocialMarketingStatusComponent>;

  beforeEach(async () => {
    const apiSpy = { getStatus: vi.fn().mockReturnValue(of({ job_id: 'j1', status: 'running' })) };
    await TestBed.configureTestingModule({
      imports: [SocialMarketingStatusComponent, NoopAnimationsModule],
      providers: [{ provide: SocialMarketingApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(SocialMarketingStatusComponent);
    component = fixture.componentInstance;
    component.jobId = 'j1';
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
