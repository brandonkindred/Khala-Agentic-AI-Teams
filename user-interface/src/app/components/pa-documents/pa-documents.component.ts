import { Component, Input, inject, OnInit, OnChanges, SimpleChanges } from '@angular/core';
import { FormBuilder, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatListModule } from '@angular/material/list';
import { MatExpansionModule } from '@angular/material/expansion';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';
import { PersonalAssistantApiService } from '../../services/personal-assistant-api.service';
import type { GeneratedDocument } from '../../models';
import { extractErrorDetail } from '../../shared/extract-error-detail';

/**
 * Documents component for generating process docs, checklists, SOPs, etc.
 */
@Component({
  selector: 'app-pa-documents',
  standalone: true,
  imports: [
    ReactiveFormsModule,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    MatSelectModule,
    MatButtonModule,
    MatIconModule,
    MatListModule,
    MatExpansionModule,
    MatSnackBarModule,
  ],
  templateUrl: './pa-documents.component.html',
  styleUrl: './pa-documents.component.scss',
})
export class PaDocumentsComponent implements OnInit, OnChanges {
  @Input() userId = 'default';

  private readonly api = inject(PersonalAssistantApiService);
  private readonly fb = inject(FormBuilder);
  private readonly snackBar = inject(MatSnackBar);

  documents: GeneratedDocument[] = [];
  loading = false;
  generating = false;
  form: FormGroup;

  docTypes = [
    { value: 'process', label: 'Process Document', icon: 'article' },
    { value: 'checklist', label: 'Checklist', icon: 'checklist' },
    { value: 'template', label: 'Template', icon: 'file_copy' },
    { value: 'sop', label: 'Standard Operating Procedure', icon: 'assignment' },
    { value: 'agenda', label: 'Meeting Agenda', icon: 'groups' },
  ];

  constructor() {
    this.form = this.fb.nonNullable.group({
      docType: ['process', Validators.required],
      topic: ['', [Validators.required, Validators.minLength(5)]],
    });
  }

  ngOnInit(): void {
    this.loadDocuments();
  }

  ngOnChanges(changes: SimpleChanges): void {
    if (changes['userId'] && !changes['userId'].firstChange) {
      this.loadDocuments();
    }
  }

  private loadDocuments(): void {
    this.loading = true;
    this.api.getDocuments(this.userId).subscribe({
      next: (docs) => {
        this.documents = docs;
        this.loading = false;
      },
      error: () => {
        this.documents = [];
        this.loading = false;
      },
    });
  }

  onGenerate(): void {
    if (this.form.invalid || this.generating) return;

    const formValue = this.form.getRawValue();
    this.generating = true;

    this.api
      .generateDocument(this.userId, {
        doc_type: formValue.docType,
        topic: formValue.topic.trim(),
      })
      .subscribe({
        next: (doc) => {
          this.documents.unshift(doc);
          this.generating = false;
          this.form.patchValue({ topic: '' });
          this.snackBar.open(`Generated: ${doc.title}`, 'Close', { duration: 3000 });
        },
        error: (err) => {
          this.generating = false;
          this.snackBar.open(extractErrorDetail(err, 'Failed to generate document'), 'Close', {
            duration: 3000,
          });
        },
      });
  }

  getDocTypeLabel(type: string): string {
    return this.docTypes.find((t) => t.value === type)?.label || type;
  }

  getDocTypeIcon(type: string): string {
    return this.docTypes.find((t) => t.value === type)?.icon || 'description';
  }

  formatDate(dateStr: string): string {
    return new Date(dateStr).toLocaleDateString([], {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
    });
  }
}
