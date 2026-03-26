import { Component, OnInit, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatTabsModule } from '@angular/material/tabs';
import { MatIconModule } from '@angular/material/icon';
import { MatCardModule } from '@angular/material/card';
import { MatButtonModule } from '@angular/material/button';
import { MatChipsModule } from '@angular/material/chips';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatDividerModule } from '@angular/material/divider';
import { MatButtonToggleModule } from '@angular/material/button-toggle';

import { InvestmentApiService } from '../../services/investment-api.service';
import { InvestmentChatComponent } from '../investment-chat/investment-chat.component';
import { InvestmentProfileFormComponent } from '../investment-profile-form/investment-profile-form.component';
import { InvestmentProposalComponent } from '../investment-proposal/investment-proposal.component';
import { InvestmentStrategyComponent } from '../investment-strategy/investment-strategy.component';
import { InvestmentPromotionComponent } from '../investment-promotion/investment-promotion.component';
import { InvestmentWorkflowComponent } from '../investment-workflow/investment-workflow.component';
import { StrategyLabComponent } from '../strategy-lab/strategy-lab.component';
import {
  IPS,
  PortfolioProposal,
  StrategySpec,
  PromotionDecision,
} from '../../models';

@Component({
  selector: 'app-investment-dashboard',
  standalone: true,
  imports: [
    CommonModule,
    MatTabsModule,
    MatIconModule,
    MatCardModule,
    MatButtonModule,
    MatChipsModule,
    MatTooltipModule,
    MatDividerModule,
    MatButtonToggleModule,
    InvestmentChatComponent,
    InvestmentProfileFormComponent,
    InvestmentProposalComponent,
    InvestmentStrategyComponent,
    InvestmentPromotionComponent,
    InvestmentWorkflowComponent,
    StrategyLabComponent,
  ],
  templateUrl: './investment-dashboard.component.html',
  styleUrl: './investment-dashboard.component.scss',
})
export class InvestmentDashboardComponent implements OnInit {
  private readonly api = inject(InvestmentApiService);

  viewMode: 'chat' | 'forms' = 'chat';
  selectedTabIndex = 0;
  healthStatus: 'checking' | 'healthy' | 'unhealthy' = 'checking';

  currentIPS: IPS | null = null;
  currentProposal: PortfolioProposal | null = null;
  currentStrategy: StrategySpec | null = null;
  lastDecision: PromotionDecision | null = null;

  showProfileForm = false;

  ngOnInit(): void {
    this.checkHealth();
  }

  checkHealth(): void {
    this.healthStatus = 'checking';
    this.api.healthCheck().subscribe({
      next: () => {
        this.healthStatus = 'healthy';
      },
      error: () => {
        this.healthStatus = 'unhealthy';
      },
    });
  }

  onProfileCreated(ips: IPS): void {
    this.currentIPS = ips;
    this.showProfileForm = false;
    this.selectedTabIndex = 1;
  }

  onProfileFormCancelled(): void {
    this.showProfileForm = false;
  }

  onProposalCreated(proposal: PortfolioProposal): void {
    this.currentProposal = proposal;
  }

  onStrategyCreated(strategy: StrategySpec): void {
    this.currentStrategy = strategy;
  }

  onDecisionMade(decision: PromotionDecision): void {
    this.lastDecision = decision;
  }

  loadProfile(userId: string): void {
    this.api.getProfile(userId).subscribe({
      next: (response) => {
        if (response.found && response.ips) {
          this.currentIPS = response.ips;
        }
      },
    });
  }

  clearProfile(): void {
    this.currentIPS = null;
    this.currentProposal = null;
    this.currentStrategy = null;
    this.lastDecision = null;
  }
}
