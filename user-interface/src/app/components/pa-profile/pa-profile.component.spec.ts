import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { PersonalAssistantApiService } from '../../services/personal-assistant-api.service';
import { PaProfileComponent } from './pa-profile.component';

describe('PaProfileComponent', () => {
  let component: PaProfileComponent;
  let fixture: ComponentFixture<PaProfileComponent>;

  beforeEach(async () => {
    const apiSpy = { getProfile: vi.fn().mockReturnValue(of({ identity: {}, preferences: {} })) };
    await TestBed.configureTestingModule({
      imports: [PaProfileComponent, NoopAnimationsModule],
      providers: [{ provide: PersonalAssistantApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(PaProfileComponent);
    component = fixture.componentInstance;
    component.userId = 'u1';
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
