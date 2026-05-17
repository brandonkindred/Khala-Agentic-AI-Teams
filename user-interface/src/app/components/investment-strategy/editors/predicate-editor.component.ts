import { Component, Input } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormGroup, ReactiveFormsModule } from '@angular/forms';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatSelectModule } from '@angular/material/select';

import { COMPARISON_OP_OPTIONS } from '../../../models';
import { PredicateSideEditorComponent } from './predicate-side-editor.component';

@Component({
  selector: 'app-predicate-editor',
  standalone: true,
  imports: [
    CommonModule,
    ReactiveFormsModule,
    MatFormFieldModule,
    MatSelectModule,
    PredicateSideEditorComponent,
  ],
  templateUrl: './predicate-editor.component.html',
})
export class PredicateEditorComponent {
  @Input({ required: true }) group!: FormGroup;

  readonly opOptions = COMPARISON_OP_OPTIONS;

  get lhsGroup(): FormGroup {
    return this.group.get('lhs') as FormGroup;
  }

  get rhsGroup(): FormGroup {
    return this.group.get('rhs') as FormGroup;
  }
}
