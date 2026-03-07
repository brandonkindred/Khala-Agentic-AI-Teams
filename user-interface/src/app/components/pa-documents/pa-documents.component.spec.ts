import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { PersonalAssistantApiService } from '../../services/personal-assistant-api.service';
import { PaDocumentsComponent } from './pa-documents.component';

describe('PaDocumentsComponent', () => {
  let component: PaDocumentsComponent;
  let fixture: ComponentFixture<PaDocumentsComponent>;

  beforeEach(async () => {
    const apiSpy = { getDocuments: vi.fn().mockReturnValue(of([])) };
    await TestBed.configureTestingModule({
      imports: [PaDocumentsComponent, NoopAnimationsModule],
      providers: [{ provide: PersonalAssistantApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(PaDocumentsComponent);
    component = fixture.componentInstance;
    component.userId = 'u1';
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
