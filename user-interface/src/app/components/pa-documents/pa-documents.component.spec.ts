import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { SimpleChange } from '@angular/core';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { PersonalAssistantApiService } from '../../services/personal-assistant-api.service';
import { PaDocumentsComponent } from './pa-documents.component';

describe('PaDocumentsComponent', () => {
  let component: PaDocumentsComponent;
  let fixture: ComponentFixture<PaDocumentsComponent>;
  let apiSpy: {
    getDocuments: ReturnType<typeof vi.fn>;
    generateDocument: ReturnType<typeof vi.fn>;
  };

  beforeEach(async () => {
    apiSpy = {
      getDocuments: vi.fn().mockReturnValue(of([])),
      generateDocument: vi.fn().mockReturnValue(of({ title: 'Doc', doc_type: 'process' })),
    };
    await TestBed.configureTestingModule({
      imports: [PaDocumentsComponent, NoopAnimationsModule],
      providers: [{ provide: PersonalAssistantApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(PaDocumentsComponent);
    component = fixture.componentInstance;
    component.userId = 'u1';
    fixture.detectChanges();
  });

  it('creates and loads documents', () => {
    expect(component).toBeTruthy();
    expect(apiSpy.getDocuments).toHaveBeenCalledWith('u1');
  });

  it('loadDocuments handles error', () => {
    apiSpy.getDocuments.mockReturnValue(throwError(() => new Error('boom')));
    (component as unknown as { loadDocuments: () => void }).loadDocuments();
    expect(component.documents).toEqual([]);
    expect(component.loading).toBe(false);
  });

  it('ngOnChanges reloads on userId change', () => {
    apiSpy.getDocuments.mockClear();
    component.ngOnChanges({ userId: new SimpleChange('u1', 'u2', false) });
    expect(apiSpy.getDocuments).toHaveBeenCalled();
  });

  it('ngOnChanges ignores first change', () => {
    apiSpy.getDocuments.mockClear();
    component.ngOnChanges({ userId: new SimpleChange(undefined, 'u1', true) });
    expect(apiSpy.getDocuments).not.toHaveBeenCalled();
  });

  it('onGenerate does nothing if invalid', () => {
    component.form.setValue({ docType: 'process', topic: 'abc' });
    component.onGenerate();
    expect(apiSpy.generateDocument).not.toHaveBeenCalled();
  });

  it('onGenerate does nothing if generating', () => {
    component.form.setValue({ docType: 'process', topic: 'My new doc topic' });
    component.generating = true;
    component.onGenerate();
    expect(apiSpy.generateDocument).not.toHaveBeenCalled();
  });

  it('onGenerate posts and unshifts doc', () => {
    component.form.setValue({ docType: 'sop', topic: 'My new doc topic' });
    component.onGenerate();
    expect(apiSpy.generateDocument).toHaveBeenCalledWith('u1', {
      doc_type: 'sop',
      topic: 'My new doc topic',
    });
    expect(component.documents.length).toBe(1);
    expect(component.generating).toBe(false);
  });

  it('onGenerate error handled', () => {
    apiSpy.generateDocument.mockReturnValue(throwError(() => ({ error: { detail: 'oops' } })));
    component.form.setValue({ docType: 'process', topic: 'My new doc topic' });
    component.onGenerate();
    expect(component.generating).toBe(false);
  });

  it('getDocTypeLabel returns label or type', () => {
    expect(component.getDocTypeLabel('process')).toBe('Process Document');
    expect(component.getDocTypeLabel('unknown')).toBe('unknown');
  });

  it('getDocTypeIcon returns icon or fallback', () => {
    expect(component.getDocTypeIcon('process')).toBe('article');
    expect(component.getDocTypeIcon('unknown')).toBe('description');
  });

  it('formatDate returns formatted string', () => {
    expect(component.formatDate('2025-06-15T00:00:00Z').length).toBeGreaterThan(0);
  });
});
