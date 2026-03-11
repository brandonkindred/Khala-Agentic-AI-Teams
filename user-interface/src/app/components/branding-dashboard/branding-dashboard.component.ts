import { Component, OnDestroy, inject } from '@angular/core';
import { FormBuilder, FormsModule, ReactiveFormsModule, Validators } from '@angular/forms';
import { interval, Subscription, switchMap } from 'rxjs';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatButtonModule } from '@angular/material/button';
import { MatTabsModule } from '@angular/material/tabs';
import { MatIconModule } from '@angular/material/icon';
import { MatOption, MatSelectModule } from '@angular/material/select';
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
    LoadingSpinnerComponent,
    ErrorMessageComponent,
    HealthIndicatorComponent,
    BrandingChatComponent,
    BrandPreviewComponent,
  ],
  templateUrl: './branding-dashboard.component.html',
  styleUrl: './branding-dashboard.component.scss',
})
export class BrandingDashboardComponent implements OnDestroy {
  private readonly api = inject(BrandingApiService);
  private readonly fb = inject(FormBuilder);

  conversationMission: BrandingMissionSnapshot | null = null;
  conversationLatestOutput: BrandingTeamOutput | null = null;

  loading = false;
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
    this.conversationMission = state.mission;
    this.conversationLatestOutput = state.latest_output;
  }

  onSaveToAgency(): void {
    this.showSaveToAgencyFlow = true;
    this.loadClients();
  }

  showSaveToAgencyFlow = false;
  saveToAgencyClientId: string | null = null;
  saveToAgencyBrandName = '';
  saveToAgencyNewClientName = '';
  saveToAgencyError: string | null = null;
  saveToAgencySuccess: string | null = null;

  closeSaveToAgency(): void {
    this.showSaveToAgencyFlow = false;
    this.saveToAgencyClientId = null;
    this.saveToAgencyBrandName = '';
    this.saveToAgencyNewClientName = '';
    this.saveToAgencyError = null;
    this.saveToAgencySuccess = null;
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
        this.saveToAgencyError = err?.error?.detail ?? err?.message ?? 'Failed to create client';
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
      this.saveToAgencyError = 'Select or create a client first.';
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
        this.api.runBrand(clientId, brand.id).subscribe({
          next: () => {
            this.saveToAgencySuccess = `Brand "${brand.name}" saved and run completed.`;
            this.closeSaveToAgency();
            if (this.selectedClient?.id === clientId) {
              this.api.listBrands(clientId).subscribe({ next: (list) => (this.brands = list) });
            }
          },
          error: (err) => {
            this.saveToAgencySuccess = `Brand "${brand.name}" created. Run failed: ${err?.error?.detail ?? err?.message}`;
          },
        });
      },
      error: (err) => {
        this.saveToAgencyError = err?.error?.detail ?? err?.message ?? 'Failed to create brand';
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
        this.clientLoadError = err?.error?.detail ?? err?.message ?? 'Failed to load clients';
      },
    });
  }

  selectClient(client: Client): void {
    this.selectedClient = client;
    this.selectedBrand = null;
    this.brands = [];
    this.brandActionMessage = null;
    this.api.listBrands(client.id).subscribe({
      next: (list) => (this.brands = list),
      error: () => (this.brands = []),
    });
  }

  createClient(): void {
    const name = this.newClientName.trim();
    if (!name) return;
    this.loading = true;
    this.error = null;
    this.api.createClient({ name }).subscribe({
      next: () => {
        this.newClientName = '';
        this.loading = false;
        this.loadClients();
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Failed to create client';
        this.loading = false;
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
    this.loading = true;
    this.error = null;
    this.api.createBrand(this.selectedClient.id, request).subscribe({
      next: (brand) => {
        this.brands = [...this.brands, brand];
        this.showCreateBrand = false;
        this.newBrandForm.reset({ company_name: '', company_description: '', target_audience: '', name: '' });
        this.loading = false;
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Failed to create brand';
        this.loading = false;
      },
    });
  }

  runBrand(brand: Brand): void {
    if (!this.selectedClient) return;
    this.loading = true;
    this.brandActionMessage = null;
    this.api.runBrand(this.selectedClient.id, brand.id).subscribe({
      next: () => {
        this.brandActionMessage = 'Brand run completed. Output saved to brand version.';
        this.loading = false;
        this.api.getBrand(this.selectedClient!.id, brand.id).subscribe({
          next: (updated) => {
            this.brands = this.brands.map((b) => (b.id === brand.id ? updated : b));
            this.selectedBrand = this.selectedBrand?.id === brand.id ? updated : this.selectedBrand;
          },
        });
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Run failed';
        this.loading = false;
      },
    });
  }

  requestMarketResearchForBrand(brand: Brand): void {
    if (!this.selectedClient) return;
    this.loading = true;
    this.brandActionMessage = null;
    this.api.requestMarketResearch(this.selectedClient.id, brand.id).subscribe({
      next: (snapshot) => {
        this.brandActionMessage = `Market research: ${snapshot.summary.slice(0, 80)}...`;
        this.loading = false;
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Market research request failed';
        this.loading = false;
      },
    });
  }

  requestDesignAssetsForBrand(brand: Brand): void {
    if (!this.selectedClient) return;
    this.loading = true;
    this.brandActionMessage = null;
    this.api.requestDesignAssets(this.selectedClient.id, brand.id).subscribe({
      next: (result) => {
        this.brandActionMessage = `Design request ${result.request_id} (${result.status}).`;
        this.loading = false;
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Design assets request failed';
        this.loading = false;
      },
    });
  }

  ngOnDestroy(): void {
    this.pollSub?.unsubscribe();
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
