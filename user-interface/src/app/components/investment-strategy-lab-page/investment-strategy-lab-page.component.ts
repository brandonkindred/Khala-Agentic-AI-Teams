import { ChangeDetectionStrategy, Component, OnInit, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { StrategyLabComponent } from '../strategy-lab/strategy-lab.component';
import { InvestmentApiService } from '../../services/investment-api.service';

@Component({
  selector: 'app-investment-strategy-lab-page',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, MatButtonModule, MatIconModule, StrategyLabComponent],
  templateUrl: './investment-strategy-lab-page.component.html',
  styleUrl: './investment-strategy-lab-page.component.scss',
})
export class InvestmentStrategyLabPageComponent implements OnInit {
  private readonly api = inject(InvestmentApiService);

  readonly healthStatus = signal<'checking' | 'healthy' | 'unhealthy'>('checking');

  ngOnInit(): void {
    this.api.healthCheck().subscribe({
      next: () => {
        this.healthStatus.set('healthy');
      },
      error: () => {
        this.healthStatus.set('unhealthy');
      },
    });
  }
}
