import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { PersonalAssistantApiService } from '../../services/personal-assistant-api.service';
import { PaTasksComponent } from './pa-tasks.component';

describe('PaTasksComponent', () => {
  let component: PaTasksComponent;
  let fixture: ComponentFixture<PaTasksComponent>;

  beforeEach(async () => {
    const apiSpy = { getTasks: vi.fn().mockReturnValue(of([])) };
    await TestBed.configureTestingModule({
      imports: [PaTasksComponent, NoopAnimationsModule],
      providers: [{ provide: PersonalAssistantApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(PaTasksComponent);
    component = fixture.componentInstance;
    component.userId = 'u1';
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
