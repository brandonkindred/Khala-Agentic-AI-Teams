import { Component, OnInit, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatButtonModule } from '@angular/material/button';
import { MatSlideToggleModule } from '@angular/material/slide-toggle';
import { MatIconModule } from '@angular/material/icon';
import { IntegrationsApiService } from '../../services/integrations-api.service';
import type { SlackConfigResponse, SlackConfigUpdate } from '../../models/integrations.model';

const SLACK_WEBHOOK_PREFIX = 'https://hooks.slack.com/';

@Component({
  selector: 'app-integrations-dashboard',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    MatButtonModule,
    MatSlideToggleModule,
    MatIconModule,
  ],
  templateUrl: './integrations-dashboard.component.html',
  styleUrl: './integrations-dashboard.component.scss',
})
export class IntegrationsDashboardComponent implements OnInit {
  private readonly api = inject(IntegrationsApiService);

  loading = false;
  saving = false;
  error: string | null = null;
  success: string | null = null;

  slackEnabled = false;
  webhookUrl = '';
  channelDisplayName = '';
  webhookConfigured = false;

  ngOnInit(): void {
    this.loadSlackConfig();
  }

  loadSlackConfig(): void {
    this.loading = true;
    this.error = null;
    this.api.getSlackConfig().subscribe({
      next: (res: SlackConfigResponse) => {
        this.slackEnabled = res.enabled;
        this.webhookConfigured = res.webhook_configured;
        this.channelDisplayName = res.channel_display_name || '';
        this.webhookUrl = '';
        this.loading = false;
      },
      error: (err) => {
        this.error = err?.error?.detail || err?.message || 'Failed to load Slack config';
        this.loading = false;
      },
    });
  }

  webhookUrlInvalid(): boolean {
    const u = (this.webhookUrl || '').trim();
    if (!u) return false;
    return !u.startsWith(SLACK_WEBHOOK_PREFIX) || u.length < 50;
  }

  saveSlack(): void {
    const url = this.webhookUrl.trim();
    if (this.slackEnabled && url && this.webhookUrlInvalid()) {
      this.error = 'Webhook URL must start with https://hooks.slack.com/ and be a valid URL';
      return;
    }
    this.saving = true;
    this.error = null;
    this.success = null;
    const body: SlackConfigUpdate = {
      enabled: this.slackEnabled,
      webhook_url: url,
      channel_display_name: this.channelDisplayName.trim(),
    };
    this.api.updateSlackConfig(body).subscribe({
      next: (res) => {
        this.slackEnabled = res.enabled;
        this.webhookConfigured = res.webhook_configured;
        this.channelDisplayName = res.channel_display_name || '';
        this.success = 'Slack integration saved.';
        this.saving = false;
      },
      error: (err) => {
        this.error = err?.error?.detail || err?.message || 'Failed to save Slack config';
        this.saving = false;
      },
    });
  }
}
