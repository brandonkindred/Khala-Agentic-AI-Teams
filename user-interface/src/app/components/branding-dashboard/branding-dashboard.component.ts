import { Component, OnDestroy, OnInit, inject } from '@angular/core';
import { ActivatedRoute, Router } from '@angular/router';
import { BreakpointObserver } from '@angular/cdk/layout';
import { FormBuilder, FormsModule, ReactiveFormsModule, Validators } from '@angular/forms';
import { interval, Subscription, switchMap } from 'rxjs';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatButtonModule } from '@angular/material/button';
import { MatTabsModule } from '@angular/material/tabs';
import { MatIconModule } from '@angular/material/icon';
import { MatOption, MatSelectModule } from '@angular/material/select';
import { MatMenuModule } from '@angular/material/menu';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';
import { MatExpansionModule } from '@angular/material/expansion';
import { BrandingApiService } from '../../services/branding-api.service';
import { BrandActivityService } from '../../services/brand-activity.service';
import { LoadingSpinnerComponent } from '../../shared/loading-spinner/loading-spinner.component';
import { ErrorMessageComponent } from '../../shared/error-message/error-message.component';
import { HealthIndicatorComponent } from '../health-indicator/health-indicator.component';
import { BrandingChatComponent } from '../branding-chat/branding-chat.component';
import { BrandPreviewComponent } from '../brand-preview/brand-preview.component';
import { BrandActivityStripComponent } from '../brand-activity-strip/brand-activity-strip.component';
import { BrandingContextSelectorComponent } from './branding-context-selector/branding-context-selector.component';
import type {
  Brand,
  BrandActivity,
  BrandingMissionSnapshot,
  BrandingQuestion,
  BrandingSessionResponse,
  BrandingTeamOutput,
  Client,
  CreateBrandRequest,
  RunBrandingTeamRequest,
} from '../../models';
import type { BrandingChatState } from '../branding-chat/branding-chat.component';

/** Default client name for the implicit single-workspace model (API still uses /clients/:id/brands). */
const WORKSPACE_CLIENT_NAME = 'My brands';

@Component({
  selector: 'app-branding-dashboard',
  standalone: true,
  imports: [
    FormsModule,
    ReactiveFormsModule,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    MatButtonModule,
    MatTabsModule,
    MatIconModule,
    MatSelectModule,
    MatOption,
    MatMenuModule,
    MatSnackBarModule,
    MatExpansionModule,
    LoadingSpinnerComponent,
    ErrorMessageComponent,
    HealthIndicatorComponent,
    BrandingChatComponent,
    BrandPreviewComponent,
    BrandActivityStripComponent,
    BrandingContextSelectorComponent,
  ],
  templateUrl: './branding-dashboard.component.html',
  styleUrl: './branding-dashboard.component.scss',
})
export class BrandingDashboardComponent implements OnInit, OnDestroy {
  private readonly api = inject(BrandingApiService);
  private readonly fb = inject(FormBuilder);
  private readonly snackBar = inject(MatSnackBar);
  private readonly breakpoint = inject(BreakpointObserver);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly activityStore = inject(BrandActivityService);
  /** Per-activity-id polling subscriptions so we can clean up on destroy. */
  private readonly activityPolls = new Map<string, Subscription>();

  /** Narrow layout: collapsible brand preview panel. */
  isCompactLayout = false;
  private layoutSub: Subscription | null = null;

  conversationMission: BrandingMissionSnapshot | null = null;
  conversationLatestOutput: BrandingTeamOutput | null = null;
  activeConversationId: string | null = null;

  /** True during initial workspace bootstrap or heavy session operations (full-page spinner). */
  loading = false;
  /** True while creating a brand or running brand actions from the Form tab (button-level only). */
  brandFormBusy = false;
  error: string | null = null;
  session: BrandingSessionResponse | null = null;
  answers: Record<string, string> = {};
  private pollSub: Subscription | null = null;

  clients: Client[] = [];
  selectedClient: Client | null = null;
  brands: Brand[] = [];
  selectedBrand: Brand | null = null;
  clientLoadError: string | null = null;
  brandActionMessage: string | null = null;
  newClientName = '';
  showCreateBrand = false;
  /** Brief highlight on the row for a newly created brand (scroll target). */
  highlightedBrandId: string | null = null;

  selectedTabIndex = 0;

  newBrandForm = this.fb.nonNullable.group({
    company_name: ['', [Validators.required, Validators.minLength(2)]],
    company_description: ['', [Validators.required, Validators.minLength(10)]],
    target_audience: ['', [Validators.required, Validators.minLength(3)]],
    name: [''],
  });

  form = this.fb.nonNullable.group({
    company_name: ['', [Validators.required, Validators.minLength(2)]],
    company_description: ['', [Validators.required, Validators.minLength(10)]],
    target_audience: ['', [Validators.required, Validators.minLength(3)]],
    desired_voice: ['clear, confident, human', [Validators.required]],
    values_csv: [''],
    differentiators_csv: [''],
  });

  healthCheck = (): ReturnType<BrandingApiService['health']> => this.api.health();

  onChatStateChange(state: BrandingChatState): void {
    this.activeConversationId = state.conversation_id;
    this.conversationMission = state.mission;
    this.conversationLatestOutput = state.latest_output;
    this.syncBrandPreviewFromSelection();
    this.syncQueryParams();
  }

  /** Handle auto-created brand from chat: refresh brands and select it. */
  onBrandAutoCreated(brandId: string): void {
    if (!this.selectedClient) return;
    this.api.listBrands(this.selectedClient.id).subscribe({
      next: (brands) => {
        this.brands = brands;
        const created = brands.find((b) => b.id === brandId);
        if (created) {
          this.selectedBrand = created;
          this.conversationMission = created.mission;
          this.snackBar.open(
            `Brand "${created.name}" auto-created from your conversation.`,
            'Dismiss',
            { duration: 6000 }
          );
        }
      },
    });
  }

  /** Keep URL query params in sync so the user can bookmark / deep-link back. */
  private syncQueryParams(): void {
    const params: Record<string, string> = {};
    if (this.selectedClient?.id) params['workspaceId'] = this.selectedClient.id;
    if (this.activeConversationId) params['conversationId'] = this.activeConversationId;
    if (this.selectedBrand?.id) params['brandId'] = this.selectedBrand.id;
    this.router.navigate([], {
      relativeTo: this.route,
      queryParams: params,
      queryParamsHandling: 'replace',
      replaceUrl: true,
    });
  }

  onWorkspaceChange(client: Client): void {
    this.selectClient(client);
  }

  onBrandChange(brand: Brand): void {
    this.resumeOrStartBrand(brand);
  }

  onAddClientFromSelector(name: string): void {
    this.newClientName = name;
    this.createClient();
  }

  onOpenSaveAsBrand(): void {
    this.showSaveAsBrandDialog = true;
    this.saveToAgencyClientId = this.selectedClient?.id ?? null;
    this.saveToAgencyError = null;
    this.loadClients();
  }

  showSaveAsBrandDialog = false;
  saveToAgencyClientId: string | null = null;
  saveToAgencyBrandName = '';
  saveToAgencyNewClientName = '';
  saveToAgencyError: string | null = null;
  saveToAgencySuccess: string | null = null;
  saveModalAdvanced = false;

  closeSaveAsBrandDialog(): void {
    this.showSaveAsBrandDialog = false;
    this.saveToAgencyClientId = null;
    this.saveToAgencyBrandName = '';
    this.saveToAgencyNewClientName = '';
    this.saveToAgencyError = null;
    this.saveToAgencySuccess = null;
    this.saveModalAdvanced = false;
  }

  createClientForSave(): void {
    const name = this.saveToAgencyNewClientName.trim();
    if (!name) return;
    this.api.createClient({ name }).subscribe({
      next: (client) => {
        this.clients = [...this.clients, client];
        this.saveToAgencyClientId = client.id;
        this.saveToAgencyNewClientName = '';
        this.loadClients();
      },
      error: (err) => {
        this.saveToAgencyError = err?.error?.detail ?? err?.message ?? 'Failed to create workspace';
      },
    });
  }

  saveConversationToAgency(): void {
    const mission = this.conversationMission;
    if (!mission) {
      this.saveToAgencyError = 'No mission to save.';
      return;
    }
    const clientId = this.saveToAgencyClientId ?? this.selectedClient?.id;
    if (!clientId) {
      this.saveToAgencyError = 'Workspace is not ready. Refresh the page and try again.';
      return;
    }
    const brandName = this.saveToAgencyBrandName.trim() || mission.company_name;
    const request: CreateBrandRequest = {
      company_name: mission.company_name,
      company_description: mission.company_description,
      target_audience: mission.target_audience,
      name: brandName,
      values: mission.values,
      differentiators: mission.differentiators,
      desired_voice: mission.desired_voice,
      existing_brand_material: mission.existing_brand_material,
      conversation_id: this.activeConversationId,
    };
    this.saveToAgencyError = null;
    this.api.createBrand(clientId, request).subscribe({
      next: (brand) => {
        // Switch to the brand's conversation (existing chat is now attached).
        this.activeConversationId = brand.conversation_id ?? null;
        this.api.runBrand(clientId, brand.id).subscribe({
          next: () => {
            this.snackBar.open(`Brand "${brand.name}" saved and run completed.`, 'Dismiss', { duration: 5000 });
            this.closeSaveAsBrandDialog();
            if (this.selectedClient?.id === clientId) {
              this.api.listBrands(clientId).subscribe({
                next: (list) => {
                  this.brands = list;
                  this.selectedBrand = this.brands.find((b) => b.id === brand.id) ?? brand;
                  this.applyDefaultBrandSelection();
                },
              });
            }
          },
          error: (err) => {
            this.snackBar.open(
              `Brand "${brand.name}" created. Run failed: ${err?.error?.detail ?? err?.message}`,
              'Dismiss',
              { duration: 8000 }
            );
          },
        });
      },
      error: (err) => {
        this.saveToAgencyError = err?.error?.detail ?? err?.message ?? 'Failed to create brand';
      },
    });
  }

  /** Conversation/brand/workspace IDs from URL query params, used to restore state on page load. */
  private pendingConversationId: string | null = null;
  private pendingBrandId: string | null = null;
  private pendingWorkspaceId: string | null = null;

  ngOnInit(): void {
    const snap = this.route.snapshot.queryParamMap;
    this.pendingConversationId = snap.get('conversationId');
    this.pendingBrandId = snap.get('brandId');
    this.pendingWorkspaceId = snap.get('workspaceId');
    this.ensureWorkspaceClient();
    this.layoutSub = this.breakpoint.observe('(max-width: 900px)').subscribe((state) => {
      this.isCompactLayout = state.matches;
    });
  }

  /**
   * Ensure a single implicit workspace client exists, then select it and load brands.
   * Users never manage "clients"; the API still nests brands under one client row.
   */
  ensureWorkspaceClient(): void {
    this.clientLoadError = null;
    this.loading = true;
    this.api.listClients().subscribe({
      next: (list) => {
        if (list.length === 0) {
          this.api.createClient({ name: WORKSPACE_CLIENT_NAME }).subscribe({
            next: () => {
              this.api.listClients().subscribe({
                next: (inner) => {
                  this.clients = inner;
                  if (inner.length > 0) {
                    this.selectClient(inner[0]);
                  }
                  this.loading = false;
                },
                error: (err) => {
                  this.clientLoadError = err?.error?.detail ?? err?.message ?? 'Failed to load workspace';
                  this.loading = false;
                },
              });
            },
            error: (err) => {
              this.clientLoadError = err?.error?.detail ?? err?.message ?? 'Failed to create workspace';
              this.loading = false;
            },
          });
        } else {
          this.clients = list;
          if (!this.selectedClient) {
            const target =
              (this.pendingWorkspaceId &&
                list.find((c) => c.id === this.pendingWorkspaceId)) ||
              list[0];
            this.pendingWorkspaceId = null;
            this.selectClient(target);
          } else {
            this.loading = false;
          }
        }
      },
      error: (err) => {
        this.clientLoadError = err?.error?.detail ?? err?.message ?? 'Failed to load workspace';
        this.loading = false;
      },
    });
  }

  loadClients(): void {
    this.clientLoadError = null;
    this.api.listClients().subscribe({
      next: (list) => {
        this.clients = list;
        if (list.length > 0 && !this.selectedClient) {
          this.selectClient(list[0]);
        }
      },
      error: (err) => {
        this.clientLoadError = err?.error?.detail ?? err?.message ?? 'Failed to load workspace';
      },
    });
  }

  selectClient(client: Client): void {
    this.selectedClient = client;
    this.selectedBrand = null;
    this.brands = [];
    this.brandActionMessage = null;
    this.syncQueryParams();
    this.api.listBrands(client.id).subscribe({
      next: (list) => {
        this.brands = list;
        this.applyDefaultBrandSelection();
        this.syncBrandPreviewFromSelection();
        this.hydrateRunningJobs();
        this.loading = false;
      },
      error: () => {
        this.brands = [];
        this.loading = false;
      },
    });
  }

  /**
   * When brands load and none selected, prefer the last item as "most recently created"
   * (typical API ordering). Keeps chat scoped without asking the user to pick first.
   *
   * If query params specify a brand/conversation, restore those first.
   */
  private applyDefaultBrandSelection(): void {
    if (this.brands.length === 0) {
      this.selectedBrand = null;
      return;
    }

    // Restore from URL query params on first load.
    if (this.pendingBrandId) {
      const match = this.brands.find((b) => b.id === this.pendingBrandId);
      if (match) {
        this.resumeOrStartBrand(match);
        this.pendingBrandId = null;
        this.pendingConversationId = null;
        return;
      }
    }
    this.pendingBrandId = null;
    this.pendingConversationId = null;

    if (!this.selectedBrand) {
      const last = this.brands[this.brands.length - 1];
      this.resumeOrStartBrand(last);
      return;
    }
    const stillExists = this.brands.some((b) => b.id === this.selectedBrand!.id);
    if (!stillExists) {
      const last = this.brands[this.brands.length - 1];
      this.resumeOrStartBrand(last);
    }
  }

  /**
   * Select a brand and try to resume the most recent conversation for it.
   * Falls back to creating a new conversation if none exist.
   */
  /** Select a brand and open its single permanent conversation. */
  private resumeOrStartBrand(brand: Brand): void {
    this.selectedBrand = brand;
    this.conversationMission = brand.mission;
    this.conversationLatestOutput = (brand.latest_output as BrandingTeamOutput | null) ?? null;
    this.activeConversationId = brand.conversation_id ?? null;
    this.syncQueryParams();
  }

  selectBrandForChat(brand: Brand): void {
    this.resumeOrStartBrand(brand);
  }

  openFormTabForNewBrand(): void {
    this.selectedTabIndex = 1;
    this.showCreateBrand = true;
  }

  private scrollBrandRowIntoView(brandId: string): void {
    const el = document.querySelector(`[data-brand-id="${brandId}"]`);
    el?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }

  get canCreateBrandFromChat(): boolean {
    return !!this.activeConversationId && !!this.conversationMission;
  }

  /** Deselect current brand and start a fresh unattached conversation for a new brand. */
  startFreshConversation(): void {
    this.selectedBrand = null;
    this.activeConversationId = null;
    this.conversationMission = null;
    this.conversationLatestOutput = null;
    this.syncQueryParams();
  }



  private syncBrandPreviewFromSelection(): void {
    if (!this.selectedBrand) return;
    const fresh = this.brands.find((b) => b.id === this.selectedBrand!.id);
    if (fresh) {
      this.selectedBrand = fresh;
      this.conversationLatestOutput =
        (fresh.latest_output as BrandingTeamOutput | null) ?? this.conversationLatestOutput;
    }
  }

  createClient(): void {
    const name = this.newClientName.trim();
    if (!name) return;
    this.brandFormBusy = true;
    this.error = null;
    this.api.createClient({ name }).subscribe({
      next: () => {
        this.newClientName = '';
        this.brandFormBusy = false;
        this.loadClients();
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Failed to add workspace';
        this.brandFormBusy = false;
      },
    });
  }

  createBrand(): void {
    if (!this.selectedClient || this.newBrandForm.invalid) return;
    const raw = this.newBrandForm.getRawValue();
    const request: CreateBrandRequest = {
      company_name: raw.company_name,
      company_description: raw.company_description,
      target_audience: raw.target_audience,
      name: raw.name || undefined,
    };
    this.brandFormBusy = true;
    this.error = null;
    this.api.createBrand(this.selectedClient.id, request).subscribe({
      next: (brand) => {
        this.brands = [...this.brands, brand];
        this.showCreateBrand = false;
        this.newBrandForm.reset({ company_name: '', company_description: '', target_audience: '', name: '' });
        this.brandFormBusy = false;
        this.selectedBrand = brand;
        this.highlightedBrandId = brand.id;
        setTimeout(() => {
          this.highlightedBrandId = null;
        }, 2500);
        this.resumeOrStartBrand(brand);
        this.selectedTabIndex = 0;
        const ref = this.snackBar.open(
          `Brand “${brand.name}” created. Chat is scoped to this brand.`,
          'View in Form',
          { duration: 8000 }
        );
        ref.onAction().subscribe(() => {
          this.selectedTabIndex = 1;
          setTimeout(() => this.scrollBrandRowIntoView(brand.id), 150);
        });
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Failed to create brand';
        this.brandFormBusy = false;
      },
    });
  }

  /**
   * True when this brand currently has a running or queued activity of any
   * kind. Disables the split-button so users can't double-fire pipelines for
   * the same brand.
   */
  isGenerating(brandId: string): boolean {
    return this.activityStore
      .snapshot()
      .some(
        (a) => a.brandId === brandId && (a.status === 'running' || a.status === 'queued')
      );
  }

  runBrand(brand: Brand): void {
    if (!this.selectedClient) return;
    const activity = this.activityStore.start('run', brand.id);
    this.brandActionMessage = null;
    const clientId = this.selectedClient.id;
    this.api.submitRun(clientId, brand.id).subscribe({
      next: (submission) => {
        this.activityStore.update(activity.id, {
          jobId: submission.job_id,
          status: 'queued',
        });
        this.trackRunActivity(clientId, brand, activity.id, submission.job_id);
      },
      error: (err) => {
        this.finishActivityWithError(activity.id, err, 'Run failed');
      },
    });
  }

  requestMarketResearchForBrand(brand: Brand): void {
    if (!this.selectedClient) return;
    const activity = this.activityStore.start('research', brand.id);
    this.activityStore.update(activity.id, { status: 'running' });
    this.brandActionMessage = null;
    this.api.requestMarketResearch(this.selectedClient.id, brand.id).subscribe({
      next: (snapshot) => {
        this.activityStore.update(activity.id, {
          status: 'completed',
          completedAt: new Date().toISOString(),
        });
        this.brandActionMessage = `Market research: ${snapshot.summary.slice(0, 80)}...`;
        this.snackBar.open(this.brandActionMessage, 'Dismiss', { duration: 6000 });
      },
      error: (err) => {
        this.finishActivityWithError(activity.id, err, 'Market research request failed');
      },
    });
  }

  requestDesignAssetsForBrand(brand: Brand): void {
    if (!this.selectedClient) return;
    const activity = this.activityStore.start('design', brand.id);
    this.activityStore.update(activity.id, { status: 'running' });
    this.brandActionMessage = null;
    this.api.requestDesignAssets(this.selectedClient.id, brand.id).subscribe({
      next: (result) => {
        this.activityStore.update(activity.id, {
          status: 'completed',
          completedAt: new Date().toISOString(),
        });
        this.brandActionMessage = `Design request ${result.request_id} (${result.status}).`;
        this.snackBar.open(this.brandActionMessage, 'Dismiss', { duration: 5000 });
      },
      error: (err) => {
        this.finishActivityWithError(activity.id, err, 'Design assets request failed');
      },
    });
  }

  /**
   * Subscribe to `observeJob` and mirror each status poll onto the matching
   * activity chip. On terminal success, refresh the brand so the preview panel
   * reflects the new output.
   */
  private trackRunActivity(clientId: string, brand: Brand, activityId: string, jobId: string): void {
    this.activityPolls.get(activityId)?.unsubscribe();
    const sub = this.api.observeJob(jobId).subscribe({
      next: (status) => {
        this.activityStore.applyJobStatus(activityId, status);
        if (status.status === 'completed') {
          this.api.getBrand(clientId, brand.id).subscribe({
            next: (updated) => {
              this.brands = this.brands.map((b) => (b.id === brand.id ? updated : b));
              if (this.selectedBrand?.id === brand.id) {
                this.selectedBrand = updated;
                this.conversationLatestOutput =
                  (updated.latest_output as BrandingTeamOutput | null) ?? null;
              }
              this.snackBar.open(
                `Brand "${brand.name}" run completed.`,
                'View',
                { duration: 5000 }
              ).onAction().subscribe(() => this.openActivityArtifacts(brand.id));
            },
          });
        } else if (status.status === 'failed' || status.status === 'cancelled') {
          this.snackBar.open(
            status.error || `Brand run ${status.status}.`,
            'Dismiss',
            { duration: 6000 }
          );
        }
      },
      error: (err) => this.finishActivityWithError(activityId, err, 'Run failed'),
      complete: () => {
        this.activityPolls.delete(activityId);
      },
    });
    this.activityPolls.set(activityId, sub);
  }

  private finishActivityWithError(activityId: string, err: unknown, fallback: string): void {
    const message = (err as { error?: { detail?: string }; message?: string })?.error?.detail
      ?? (err as { message?: string })?.message
      ?? fallback;
    this.activityStore.update(activityId, {
      status: 'failed',
      error: message,
      completedAt: new Date().toISOString(),
    });
    this.snackBar.open(message, 'Dismiss', { duration: 6000 });
  }

  onActivityOpen(activity: BrandActivity): void {
    this.openActivityArtifacts(activity.brandId);
  }

  onActivityRetry(brand: Brand, activity: BrandActivity): void {
    this.activityStore.remove(activity.id);
    switch (activity.kind) {
      case 'run':
        this.runBrand(brand);
        break;
      case 'research':
        this.requestMarketResearchForBrand(brand);
        break;
      case 'design':
        this.requestDesignAssetsForBrand(brand);
        break;
    }
  }

  onActivityDismiss(activity: BrandActivity): void {
    this.activityStore.remove(activity.id);
  }

  /**
   * Jump to the brand preview. Today this selects the brand and switches to
   * the Chat tab (which hosts the preview panel) — precise per-phase anchoring
   * will arrive with the phase stepper in #277.
   */
  private openActivityArtifacts(brandId: string): void {
    const brand = this.brands.find((b) => b.id === brandId);
    if (brand) {
      this.resumeOrStartBrand(brand);
    }
    this.selectedTabIndex = 0;
  }

  /**
   * Fetch in-flight jobs for the current workspace and seed the activity
   * strip so a page reload mid-run does not hide the chip.
   */
  private hydrateRunningJobs(): void {
    if (!this.brands.length) return;
    const knownBrandIds = new Set(this.brands.map((b) => b.id));
    this.api.listJobs(true).subscribe({
      next: (jobs) => {
        const before = new Set(this.activityStore.snapshot().map((a) => a.id));
        this.activityStore.hydrateFromJobs(jobs, knownBrandIds);
        for (const activity of this.activityStore.snapshot()) {
          if (before.has(activity.id)) continue;
          if (activity.kind !== 'run' || !activity.jobId) continue;
          const brand = this.brands.find((b) => b.id === activity.brandId);
          const clientId = this.selectedClient?.id;
          if (!brand || !clientId) continue;
          this.trackRunActivity(clientId, brand, activity.id, activity.jobId);
        }
      },
      error: () => {
        /* Silent: hydration is best-effort. */
      },
    });
  }

  formatConversationTime(iso: string): string {
    if (!iso) return '';
    try {
      return new Date(iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
    } catch {
      return iso;
    }
  }

  ngOnDestroy(): void {
    this.pollSub?.unsubscribe();
    this.layoutSub?.unsubscribe();
    for (const sub of this.activityPolls.values()) {
      sub.unsubscribe();
    }
    this.activityPolls.clear();
  }

  startSession(): void {
    if (this.form.invalid) {
      this.form.markAllAsTouched();
      return;
    }

    const raw = this.form.getRawValue();
    const request: RunBrandingTeamRequest = {
      company_name: raw.company_name,
      company_description: raw.company_description,
      target_audience: raw.target_audience,
      desired_voice: raw.desired_voice,
      values: raw.values_csv.split(',').map((v) => v.trim()).filter((v) => !!v),
      differentiators: raw.differentiators_csv.split(',').map((v) => v.trim()).filter((v) => !!v),
    };

    this.loading = true;
    this.error = null;
    this.api.createSession(request).subscribe({
      next: (res) => {
        this.session = res;
        this.loading = false;
        this.startPolling();
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Failed to start branding session';
        this.loading = false;
      },
    });
  }

  submitAnswer(question: BrandingQuestion): void {
    if (!this.session) return;
    const answer = (this.answers[question.id] ?? '').trim();
    if (!answer) return;

    this.loading = true;
    this.error = null;
    this.api.answerQuestion(this.session.session_id, question.id, answer).subscribe({
      next: (res) => {
        this.session = res;
        this.answers[question.id] = '';
        this.loading = false;
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Failed to submit answer';
        this.loading = false;
      },
    });
  }

  private startPolling(): void {
    this.pollSub?.unsubscribe();
    if (!this.session?.session_id) {
      return;
    }
    this.pollSub = interval(120000)
      .pipe(switchMap(() => this.api.getSession(this.session!.session_id)))
      .subscribe({
        next: (res) => {
          this.session = res;
          if (res.open_questions.length === 0) {
            this.pollSub?.unsubscribe();
            this.pollSub = null;
          }
        },
        error: () => {
          this.pollSub?.unsubscribe();
          this.pollSub = null;
        },
      });
  }
}
