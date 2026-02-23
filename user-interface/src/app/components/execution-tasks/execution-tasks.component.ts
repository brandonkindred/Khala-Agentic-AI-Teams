import { Component, OnInit } from '@angular/core';
import { JsonPipe } from '@angular/common';
import { timer } from 'rxjs';
import { switchMap } from 'rxjs/operators';
import { MatCardModule } from '@angular/material/card';
import { SoftwareEngineeringApiService } from '../../services/software-engineering-api.service';

@Component({
  selector: 'app-execution-tasks',
  standalone: true,
  imports: [MatCardModule, JsonPipe],
  templateUrl: './execution-tasks.component.html',
  styleUrl: './execution-tasks.component.scss',
})
export class ExecutionTasksComponent implements OnInit {
  data: Record<string, unknown> | null = null;

  constructor(private readonly api: SoftwareEngineeringApiService) {}

  ngOnInit(): void {
    timer(0, 60000)
      .pipe(switchMap(() => this.api.getExecutionTasks()))
      .subscribe((res) => (this.data = res));
  }
}
