import { Component, Input, inject, OnInit, OnChanges, SimpleChanges } from '@angular/core';
import { FormBuilder, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatListModule } from '@angular/material/list';
import { MatCheckboxModule } from '@angular/material/checkbox';
import { MatChipsModule } from '@angular/material/chips';
import { MatExpansionModule } from '@angular/material/expansion';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';
import { PersonalAssistantApiService } from '../../services/personal-assistant-api.service';
import type { TaskList, TaskItem } from '../../models';
import { extractErrorDetail } from '../../shared/extract-error-detail';

/**
 * Tasks component for managing task lists with natural language input.
 */
@Component({
  selector: 'app-pa-tasks',
  standalone: true,
  imports: [
    ReactiveFormsModule,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    MatButtonModule,
    MatIconModule,
    MatListModule,
    MatCheckboxModule,
    MatChipsModule,
    MatExpansionModule,
    MatSnackBarModule,
  ],
  templateUrl: './pa-tasks.component.html',
  styleUrl: './pa-tasks.component.scss',
})
export class PaTasksComponent implements OnInit, OnChanges {
  @Input() userId = 'default';

  private readonly api = inject(PersonalAssistantApiService);
  private readonly fb = inject(FormBuilder);
  private readonly snackBar = inject(MatSnackBar);

  taskLists: TaskList[] = [];
  loading = false;
  addingTasks = false;
  form: FormGroup;

  constructor() {
    this.form = this.fb.nonNullable.group({
      taskText: ['', [Validators.required, Validators.minLength(3)]],
    });
  }

  ngOnInit(): void {
    this.loadTasks();
  }

  ngOnChanges(changes: SimpleChanges): void {
    if (changes['userId'] && !changes['userId'].firstChange) {
      this.loadTasks();
    }
  }

  private loadTasks(): void {
    this.loading = true;
    this.api.getTasks(this.userId).subscribe({
      next: (lists) => {
        this.taskLists = lists;
        this.loading = false;
      },
      error: () => {
        this.taskLists = [];
        this.loading = false;
      },
    });
  }

  onAddTasks(): void {
    if (this.form.invalid || this.addingTasks) return;

    const text = this.form.getRawValue().taskText.trim();
    this.addingTasks = true;

    this.api.addTasksFromText(this.userId, { text }).subscribe({
      next: (res) => {
        this.addingTasks = false;
        this.form.reset();
        if (res.success) {
          this.snackBar.open(`Added ${res.added_items?.length || 0} items`, 'Close', { duration: 3000 });
          this.loadTasks();
        } else {
          this.snackBar.open(res.message || 'Failed to add tasks', 'Close', { duration: 3000 });
        }
      },
      error: (err) => {
        this.addingTasks = false;
        this.snackBar.open(extractErrorDetail(err, 'Failed to add tasks'), 'Close', { duration: 3000 });
      },
    });
  }

  onToggleItem(list: TaskList, item: TaskItem): void {
    const newStatus = item.status === 'completed' ? 'pending' : 'completed';
    this.api.updateTaskItem(this.userId, list.list_id, item.item_id, { status: newStatus }).subscribe({
      next: () => {
        item.status = newStatus;
      },
      error: () => {
        this.snackBar.open('Failed to update task', 'Close', { duration: 3000 });
      },
    });
  }

  getPendingCount(list: TaskList): number {
    return list.items.filter((i) => i.status !== 'completed').length;
  }

  getCompletedCount(list: TaskList): number {
    return list.items.filter((i) => i.status === 'completed').length;
  }

  getPriorityColor(priority?: string): string {
    switch (priority) {
      case 'high':
        return '#f85149';
      case 'medium':
        return '#d29922';
      case 'low':
        return '#58a6ff';
      default:
        return '#8b949e';
    }
  }
}
