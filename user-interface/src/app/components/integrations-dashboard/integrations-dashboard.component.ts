import { Component, OnInit, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatButtonModule } from '@angular/material/button';
import { MatSlideToggleModule } from '@angular/material/slide-toggle';
import { MatIconModule } from '@angular/material/icon';
import { MatRadioModule } from '@angular/material/radio';
import { IntegrationsApiService } from '../../services/integrations-api.service';
import type { SlackConfigResponse, SlackConfigUpdate, SlackMode } from '../../models/integrations.model';

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
    MatRadioModule,
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
  mode: SlackMode = 'webhook';
  webhookUrl = '';
  botToken = '';
  defaultChannel = '';
  channelDisplayName = '';
  webhookConfigured = false;
  botTokenConfigured = false;
  notifyOpenQuestions = true;
  notifyPaResponses = true;

  ngOnInit(): void {
    this.loadSlackConfig();
  }

  loadSlackConfig(): void {
    this.loading = true;
    this.error = null;
    this.api.getSlackConfig().subscribe({
      next: (res: SlackConfigResponse) => {
        this.slackEnabled = res.enabled;
        this.mode = res.mode || 'webhook';
        this.webhookConfigured = res.webhook_configured;
        this.botTokenConfigured = res.bot_token_configured;
        this.defaultChannel = res.default_channel || '';
        this.channelDisplayName = res.channel_display_name || '';
        this.notifyOpenQuestions = res.notify_open_questions ?? true;
        this.notifyPaResponses = res.notify_pa_responses ?? true;
        this.webhookUrl = '';
        this.botToken = '';
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

  botTokenInvalid(): boolean {
    const token = (this.botToken || '').trim();
    if (!token) return false;
    return !token.startsWith('xoxb-');
  }

  saveSlack(): void {
    const webhookUrl = this.webhookUrl.trim();
    const botToken = this.botToken.trim();
    const defaultChannel = this.defaultChannel.trim();

    if (this.slackEnabled && this.mode === 'webhook') {
      if (!webhookUrl && !this.webhookConfigured) {
        this.error = 'Webhook URL is required for webhook mode.';
        return;
      }
      if (webhookUrl && this.webhookUrlInvalid()) {
        this.error = 'Webhook URL must start with https://hooks.slack.com/ and be a valid URL';
        return;
      }
    }

    if (this.slackEnabled && this.mode === 'bot') {
      if (!botToken && !this.botTokenConfigured) {
        this.error = 'Bot token is required for bot mode.';
        return;
      }
      if (botToken && this.botTokenInvalid()) {
        this.error = 'Bot token must start with xoxb-';
        return;
      }
      if (!defaultChannel) {
        this.error = 'Default channel is required for bot mode.';
        return;
      }
    }

    this.saving = true;
    this.error = null;
    this.success = null;
    const body: SlackConfigUpdate = {
      enabled: this.slackEnabled,
      mode: this.mode,
      webhook_url: webhookUrl,
      bot_token: botToken,
      default_channel: defaultChannel,
      channel_display_name: this.channelDisplayName.trim(),
      notify_open_questions: this.notifyOpenQuestions,
      notify_pa_responses: this.notifyPaResponses,
    };
    this.api.updateSlackConfig(body).subscribe({
      next: (res) => {
        this.slackEnabled = res.enabled;
        this.mode = res.mode || 'webhook';
        this.webhookConfigured = res.webhook_configured;
        this.botTokenConfigured = res.bot_token_configured;
        this.defaultChannel = res.default_channel || '';
        this.channelDisplayName = res.channel_display_name || '';
        this.notifyOpenQuestions = res.notify_open_questions ?? true;
        this.notifyPaResponses = res.notify_pa_responses ?? true;
        this.webhookUrl = '';
        this.botToken = '';
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
