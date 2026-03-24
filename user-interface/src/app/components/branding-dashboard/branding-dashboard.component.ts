import { Component, ElementRef, OnDestroy, OnInit, ViewChild, inject } from '@angular/core';
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
import { LoadingSpinnerComponent } from '../../shared/loading-spinner/loading-spinner.component';
import { ErrorMessageComponent } from '../../shared/error-message/error-message.component';
import { HealthIndicatorComponent } from '../health-indicator/health-indicator.component';
import { BrandingChatComponent } from '../branding-chat/branding-chat.component';
import { BrandPreviewComponent } from '../brand-preview/brand-preview.component';
import type {
  Brand,
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

  @ViewChild('brandListWrap') private brandListWrap?: ElementRef<HTMLElement>;

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
    if (this.activeConversationId) params['conversationId'] = this.activeConversationId;
    if (this.selectedBrand?.id) params['brandId'] = this.selectedBrand.id;
    this.router.navigate([], {
      relativeTo: this.route,
      queryParams: params,
      queryParamsHandling: 'replace',
      replaceUrl: true,
    });
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
    };
    this.saveToAgencyError = null;
    this.api.createBrand(clientId, request).subscribe({
      next: (brand) => {
        // Brand creation now auto-creates a conversation on the backend.
        // Switch to the brand's permanent conversation.
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

  /** Conversation/brand IDs from URL query params, used to restore state on page load. */
  private pendingConversationId: string | null = null;
  private pendingBrandId: string | null = null;

  ngOnInit(): void {
    const snap = this.route.snapshot.queryParamMap;
    this.pendingConversationId = snap.get('conversationId');
    this.pendingBrandId = snap.get('brandId');
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
            this.selectClient(list[0]);
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
    this.api.listBrands(client.id).subscribe({
      next: (list) => {
        this.brands = list;
        this.applyDefaultBrandSelection();
        this.syncBrandPreviewFromSelection();
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

  startNewBrandConversation(brand: Brand): void {
    this.resumeOrStartBrand(brand);
  }

  openFormTabForNewBrand(): void {
    this.selectedTabIndex = 1;
    this.showCreateBrand = true;
  }

  private scrollBrandRowIntoView(brandId: string): void {
    const root = this.brandListWrap?.nativeElement;
    const el = root?.querySelector(`[data-brand-id="${brandId}"]`);
    el?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }

  get canCreateBrandFromChat(): boolean {
    return !!this.activeConversationId && !!this.conversationMission;
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
        this.startNewBrandConversation(brand);
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

  runBrand(brand: Brand): void {
    if (!this.selectedClient) return;
    this.brandFormBusy = true;
    this.brandActionMessage = null;
    this.api.runBrand(this.selectedClient.id, brand.id).subscribe({
      next: () => {
        this.brandActionMessage = 'Brand run completed. Output saved to brand version.';
        this.brandFormBusy = false;
        this.snackBar.open(this.brandActionMessage, 'Dismiss', { duration: 5000 });
        this.api.getBrand(this.selectedClient!.id, brand.id).subscribe({
          next: (updated) => {
            this.brands = this.brands.map((b) => (b.id === brand.id ? updated : b));
            this.selectedBrand = this.selectedBrand?.id === brand.id ? updated : this.selectedBrand;
          },
        });
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Run failed';
        this.brandFormBusy = false;
      },
    });
  }

  requestMarketResearchForBrand(brand: Brand): void {
    if (!this.selectedClient) return;
    this.brandFormBusy = true;
    this.brandActionMessage = null;
    this.api.requestMarketResearch(this.selectedClient.id, brand.id).subscribe({
      next: (snapshot) => {
        this.brandActionMessage = `Market research: ${snapshot.summary.slice(0, 80)}...`;
        this.brandFormBusy = false;
        this.snackBar.open(this.brandActionMessage, 'Dismiss', { duration: 6000 });
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Market research request failed';
        this.brandFormBusy = false;
      },
    });
  }

  requestDesignAssetsForBrand(brand: Brand): void {
    if (!this.selectedClient) return;
    this.brandFormBusy = true;
    this.brandActionMessage = null;
    this.api.requestDesignAssets(this.selectedClient.id, brand.id).subscribe({
      next: (result) => {
        this.brandActionMessage = `Design request ${result.request_id} (${result.status}).`;
        this.brandFormBusy = false;
        this.snackBar.open(this.brandActionMessage, 'Dismiss', { duration: 5000 });
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Design assets request failed';
        this.brandFormBusy = false;
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
