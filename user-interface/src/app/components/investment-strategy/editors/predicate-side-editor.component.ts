import { Component, Input } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormGroup, ReactiveFormsModule } from '@angular/forms';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';

import { BAR_FIELD_OPTIONS } from '../../../models';
import { IndicatorRefEditorComponent } from './indicator-ref-editor.component';

@Component({
  selector: 'app-predicate-side-editor',
  standalone: true,
  imports: [
    CommonModule,
    ReactiveFormsModule,
    MatFormFieldModule,
    MatInputModule,
    MatSelectModule,
    IndicatorRefEditorComponent,
  ],
  templateUrl: './predicate-side-editor.component.html',
})
export class PredicateSideEditorComponent {
  @Input({ required: true }) group!: FormGroup;
  @Input() allowNumber = false;
  @Input() label = 'Side';

  readonly barFields = BAR_FIELD_OPTIONS;

  get sideKind(): string {
    return (this.group.get('side_kind')?.value ?? 'indicator') as string;
  }

  get indicatorGroup(): FormGroup {
    return this.group.get('indicator') as FormGroup;
  }
}
