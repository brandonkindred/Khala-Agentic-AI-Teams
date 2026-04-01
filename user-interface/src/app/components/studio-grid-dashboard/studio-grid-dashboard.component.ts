import { Component, inject } from '@angular/core';
import { MatTabsModule } from '@angular/material/tabs';
import { MatIconModule } from '@angular/material/icon';
import { StudioGridApiService } from '../../services/studio-grid-api.service';
import { HealthIndicatorComponent } from '../health-indicator/health-indicator.component';
import { StudioGridRunPanelComponent } from '../studio-grid-run-panel/studio-grid-run-panel.component';

@Component({
  selector: 'app-studio-grid-dashboard',
  standalone: true,
  imports: [
    MatTabsModule,
    MatIconModule,
    HealthIndicatorComponent,
    StudioGridRunPanelComponent,
  ],
  templateUrl: './studio-grid-dashboard.component.html',
  styleUrl: './studio-grid-dashboard.component.scss',
})
export class StudioGridDashboardComponent {
  private readonly api = inject(StudioGridApiService);

  healthCheck = (): ReturnType<StudioGridApiService['health']> => this.api.health();
}
