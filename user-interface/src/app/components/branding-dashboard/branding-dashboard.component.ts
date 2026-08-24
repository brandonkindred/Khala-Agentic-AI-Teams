import { Component, OnDestroy, OnInit, inject } from '@angular/core';
import { ActivatedRoute, Router } from '@angular/router';
import { BreakpointObserver } from '@angular/cdk/layout';
import { FormBuilder, FormsModule, ReactiveFormsModule, Validators } from '@angular/forms';
import { Subscription } from 'rxjs';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatMenuModule } from '@angular/material/menu';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';
import { MatExpansionModule } from '@angular/material/expansion';
import { BrandingApiService } from '../../services/branding-api.service';
import { BrandActivityService } from '../../services/brand-activity.service';
import { LoadingSpinnerComponent } from '../../shared/loading-spinner/loading-spinner.component';
import { ErrorMessageComponent } from '../../shared/error-message/error-message.component';
import { extractErrorDetail } from '../../shared/extract-error-detail';
import { HealthIndicatorComponent } from '../health-indicator/health-indicator.component';
import { BrandingChatComponent } from '../branding-chat/branding-chat.component';
import { BrandPreviewComponent } from '../brand-preview/brand-preview.component';
import { BrandActivityStripComponent } from '../brand-activity-strip/brand-activity-strip.component';
import { BrandingContextSelectorComponent } from './branding-context-selector/branding-context-selector.component';
import { BrandEditPanelComponent } from './brand-edit-panel/brand-edit-panel.component';
import type {
  Brand,
  BrandActivity,
  BrandingMissionSnapshot,
  BrandingTeamOutput,
  Client,
  CreateBrandRequest,
  UpdateBrandRequest,
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
    MatIconModule,
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
    BrandEditPanelComponent,
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

  /** True during initial workspace bootstrap (full-page spinner). */
  loading = false;
  /** True while creating a brand (button-level only). */
  brandFormBusy = false;
  error: string | null = null;

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

  /** Edit details side panel state. */
  editPanelOpen = false;
  /** When true, the chat does not auto-create brands on the backend. */
  skipSave = false;

  newBrandForm = this.fb.nonNullable.group({
    company_name: ['', [Validators.required, Validators.minLength(2)]],
    company_description: ['', [Validators.required, Validators.minLength(10)]],
    target_audience: ['', [Validators.required, Validators.minLength(3)]],
    name: [''],
  });

  healthCheck = (): ReturnType<BrandingApiService['health']> => this.api.health();

  onChatStateChange(state: BrandingChatState): void {
    this.activeConversationId = state.conversation_id;
    this.conversationMission = state.mission;
    this.conversationLatestOutput = state.latest_output;
    this.syncBrandPreviewFromSelection();
    this.syncQueryParams();
  }

  onSelectPalette(index: number): void {
    if (this.conversationMission) {
      this.conversationMission = { ...this.conversationMission, selected_palette_index: index };
    }
    if (this.selectedBrand) {
      const updated: Brand = {
        ...this.selectedBrand,
        mission: { ...this.selectedBrand.mission, selected_palette_index: index },
      };
      this.selectedBrand = updated;
      this.brands = this.brands.map((b) => (b.id === updated.id ? updated : b));
    }
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
    this.closeSaveAsBrandDialog();
    this.selectClient(client);
  }

  onBrandChange(brand: Brand): void {
    this.closeSaveAsBrandDialog();
    this.resumeOrStartBrand(brand);
  }

  onAddClientFromSelector(name: string): void {
    this.newClientName = name;
    this.createClient();
  }

  onOpenSaveAsBrand(): void {
    this.showSaveAsBrandDialog = true;
    this.saveToAgencyMission = this.conversationMission;
    this.saveToAgencyError = null;
  }

  showSaveAsBrandDialog = false;
  saveToAgencyBrandName = '';
  saveToAgencyError: string | null = null;
  saveToAgencySuccess: string | null = null;
  private saveToAgencyMission: BrandingMissionSnapshot | null = null;

  closeSaveAsBrandDialog(): void {
    this.showSaveAsBrandDialog = false;
    this.saveToAgencyBrandName = '';
    this.saveToAgencyError = null;
    this.saveToAgencySuccess = null;
    this.saveToAgencyMission = null;
  }

  saveConversationToAgency(): void {
    const mission = this.saveToAgencyMission;
    if (!mission) {
      this.saveToAgencyError = 'No mission to save.';
      return;
    }
    const clientId = this.selectedClient?.id;
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
              `Brand "${brand.name}" created. Run failed: ${extractErrorDetail(err, '')}`,
              'Dismiss',
              { duration: 8000 }
            );
          },
        });
      },
      error: (err) => {
        this.saveToAgencyError = extractErrorDetail(err, 'Failed to create brand');
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
                  this.clientLoadError = extractErrorDetail(err, 'Failed to load workspace');
                  this.loading = false;
                },
              });
            },
            error: (err) => {
              this.clientLoadError = extractErrorDetail(err, 'Failed to create workspace');
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
        this.clientLoadError = extractErrorDetail(err, 'Failed to load workspace');
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
        this.clientLoadError = extractErrorDetail(err, 'Failed to load workspace');
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

  private applyDefaultBrandSelection(): void {
    if (this.brands.length === 0) {
      this.selectedBrand = null;
      return;
    }

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

  toggleEditPanel(): void {
    this.editPanelOpen = !this.editPanelOpen;
  }

  openEditPanelForNewBrand(): void {
    this.selectedBrand = null;
    this.activeConversationId = null;
    this.conversationMission = null;
    this.conversationLatestOutput = null;
    this.editPanelOpen = true;
    this.showCreateBrand = true;
    this.syncQueryParams();
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

  /** Handle mission field changes from the edit details panel. */
  onMissionUpdateFromPanel(patch: Partial<BrandingMissionSnapshot>): void {
    if (this.conversationMission) {
      this.conversationMission = { ...this.conversationMission, ...patch };
    } else {
      this.conversationMission = {
        company_name: '',
        company_description: '',
        target_audience: '',
        ...patch,
      } as BrandingMissionSnapshot;
    }
    if (this.selectedBrand && this.selectedClient) {
      const updated: Brand = {
        ...this.selectedBrand,
        mission: { ...this.selectedBrand.mission, ...patch },
      };
      this.selectedBrand = updated;
      this.brands = this.brands.map((b) => (b.id === updated.id ? updated : b));
      const req: UpdateBrandRequest = {};
      if (patch.company_name !== undefined) req.company_name = patch.company_name;
      if (patch.company_description !== undefined) req.company_description = patch.company_description;
      if (patch.target_audience !== undefined) req.target_audience = patch.target_audience;
      if (patch.desired_voice !== undefined) req.desired_voice = patch.desired_voice;
      if (patch.values !== undefined) req.values = patch.values;
      if (patch.differentiators !== undefined) req.differentiators = patch.differentiators;
      this.api.updateBrand(this.selectedClient.id, this.selectedBrand.id, req).subscribe({
        next: (refreshed) => {
          this.brands = this.brands.map((b) => (b.id === refreshed.id ? refreshed : b));
          if (this.selectedBrand?.id === refreshed.id) {
            this.selectedBrand = refreshed;
          }
        },
        error: (err) => {
          this.snackBar.open(
            extractErrorDetail(err, 'Failed to update brand'),
            'Dismiss',
            { duration: 5000 },
          );
        },
      });
      if (this.activeConversationId) {
        const summary = this.buildMissionSummaryMessage(patch);
        this.api.sendConversationMessage(this.activeConversationId, summary, this.skipSave).subscribe({
          next: (res) => {
            this.conversationMission = res.mission ?? this.conversationMission;
            this.conversationLatestOutput = res.latest_output ?? this.conversationLatestOutput;
          },
        });
      }
    } else if (this.activeConversationId) {
      const summary = this.buildMissionSummaryMessage(patch);
      this.api.sendConversationMessage(this.activeConversationId, summary, this.skipSave).subscribe({
        next: (res) => {
          this.conversationMission = res.mission ?? this.conversationMission;
          this.conversationLatestOutput = res.latest_output ?? this.conversationLatestOutput;
          if (res.brand_id && !this.selectedBrand) {
            this.onBrandAutoCreated(res.brand_id);
          }
        },
        error: () => {
          // Optimistic local update already applied; backend will reconcile on next chat turn.
        },
      });
    } else {
      const summary = this.buildMissionSummaryMessage(patch);
      this.api.createConversation(summary, this.skipSave).subscribe({
        next: (res) => {
          this.activeConversationId = res.conversation_id;
          this.conversationMission = res.mission ?? this.conversationMission;
          this.conversationLatestOutput = res.latest_output ?? this.conversationLatestOutput;
          if (res.brand_id && !this.selectedBrand) {
            this.onBrandAutoCreated(res.brand_id);
          }
          this.syncQueryParams();
        },
        error: () => {
          // Optimistic local update already applied; conversation will be created on next chat interaction.
        },
      });
    }
    this.editPanelOpen = false;
  }

  private buildMissionSummaryMessage(patch: Partial<BrandingMissionSnapshot>): string {
    const parts: string[] = [];
    if (patch.company_name) parts.push(`Our company name is ${patch.company_name}.`);
    if (patch.company_description) parts.push(`We do: ${patch.company_description}.`);
    if (patch.target_audience) parts.push(`Our target audience is ${patch.target_audience}.`);
    if (patch.desired_voice !== undefined) {
      parts.push(patch.desired_voice ? `Our desired voice is ${patch.desired_voice}.` : 'We have no specific desired voice.');
    }
    if (patch.values !== undefined) {
      parts.push(patch.values.length ? `Our values are: ${patch.values.join(', ')}.` : 'We have no specific values.');
    }
    if (patch.differentiators !== undefined) {
      parts.push(patch.differentiators.length ? `Our differentiators are: ${patch.differentiators.join(', ')}.` : 'We have no specific differentiators.');
    }
    return parts.length > 0 ? parts.join(' ') : 'I updated brand details via the edit panel.';
  }

  /** Handle the "Don't save this brand" toggle from the edit panel. */
  onSkipSaveChange(skip: boolean): void {
    this.skipSave = skip;
    if (skip) {
      this.selectedBrand = null;
      this.activeConversationId = null;
      this.conversationMission = null;
      this.conversationLatestOutput = null;
      this.syncQueryParams();
    }
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
        this.error = extractErrorDetail(err, 'Failed to add workspace');
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
        this.editPanelOpen = false;
        this.snackBar.open(
          `Brand "${brand.name}" created. Chat is scoped to this brand.`,
          'Dismiss',
          { duration: 8000 }
        );
      },
      error: (err) => {
        this.error = extractErrorDetail(err, 'Failed to create brand');
        this.brandFormBusy = false;
      },
    });
  }

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
                'Dismiss',
                { duration: 5000 }
              );
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
    const message = extractErrorDetail(err, fallback);
    this.activityStore.update(activityId, {
      status: 'failed',
      error: message,
      completedAt: new Date().toISOString(),
    });
    this.snackBar.open(message, 'Dismiss', { duration: 6000 });
  }

  onActivityOpen(activity: BrandActivity): void {
    const brand = this.brands.find((b) => b.id === activity.brandId);
    if (brand) {
      this.resumeOrStartBrand(brand);
    }
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
    this.layoutSub?.unsubscribe();
    for (const sub of this.activityPolls.values()) {
      sub.unsubscribe();
    }
    this.activityPolls.clear();
  }
}
