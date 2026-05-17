import { Component, EventEmitter, Input, Output } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormGroup, ReactiveFormsModule } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';

import { PredicateEditorComponent } from './predicate-editor.component';

@Component({
  selector: 'app-entry-rule-editor',
  standalone: true,
  imports: [
    CommonModule,
    ReactiveFormsModule,
    MatFormFieldModule,
    MatInputModule,
    MatSelectModule,
    MatButtonModule,
    MatIconModule,
    PredicateEditorComponent,
  ],
  templateUrl: './entry-rule-editor.component.html',
})
export class EntryRuleEditorComponent {
  @Input({ required: true }) group!: FormGroup;
  @Input() index = 0;
  @Output() remove = new EventEmitter<void>();

  get whenGroup(): FormGroup {
    return this.group.get('when') as FormGroup;
  }
}
