import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { PersonalAssistantApiService } from '../../services/personal-assistant-api.service';
import { PaChatComponent } from './pa-chat.component';

describe('PaChatComponent', () => {
  let component: PaChatComponent;
  let fixture: ComponentFixture<PaChatComponent>;

  beforeEach(async () => {
    const apiSpy = { sendMessage: vi.fn().mockReturnValue(of({ message: '', data: {} })) };
    await TestBed.configureTestingModule({
      imports: [PaChatComponent, NoopAnimationsModule],
      providers: [{ provide: PersonalAssistantApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(PaChatComponent);
    component = fixture.componentInstance;
    component.userId = 'u1';
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
