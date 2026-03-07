import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { PersonalAssistantApiService } from '../../services/personal-assistant-api.service';
import { PaDealsComponent } from './pa-deals.component';

describe('PaDealsComponent', () => {
  let component: PaDealsComponent;
  let fixture: ComponentFixture<PaDealsComponent>;

  beforeEach(async () => {
    const apiSpy = { getWishlist: vi.fn().mockReturnValue(of([])) };
    await TestBed.configureTestingModule({
      imports: [PaDealsComponent, NoopAnimationsModule],
      providers: [{ provide: PersonalAssistantApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(PaDealsComponent);
    component = fixture.componentInstance;
    component.userId = 'u1';
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
