import { Component, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatChipsModule } from '@angular/material/chips';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatListModule } from '@angular/material/list';
import { MatDividerModule } from '@angular/material/divider';
import { MatExpansionModule } from '@angular/material/expansion';
import { AccessibilityApiService } from '../../services/accessibility-api.service';
import type {
  DesignSystemInventoryRequest,
  DesignSystemInventoryResponse,
  DesignSystemContractRequest,
  DesignSystemContractResponse,
  Surface,
} from '../../models';
import { extractErrorDetail } from '../../shared/extract-error-detail';

interface ComponentEntry {
  name: string;
  hasContract: boolean;
  contract?: DesignSystemContractResponse;
  loading?: boolean;
}

@Component({
  selector: 'app-accessibility-design-system',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    MatSelectModule,
    MatButtonModule,
    MatIconModule,
    MatChipsModule,
    MatProgressSpinnerModule,
    MatListModule,
    MatDividerModule,
    MatExpansionModule,
  ],
  templateUrl: './accessibility-design-system.component.html',
  styleUrl: './accessibility-design-system.component.scss',
})
export class AccessibilityDesignSystemComponent {
  private readonly api = inject(AccessibilityApiService);

  systemName = '';
  source: 'storybook' | 'repo' | 'manual' = 'storybook';
  platform: Surface = 'web';

  inventory: DesignSystemInventoryResponse | null = null;
  components: ComponentEntry[] = [];

  loadingInventory = false;
  inventoryError: string | null = null;

  get canBuildInventory(): boolean {
    return !!this.systemName.trim();
  }

  buildInventory(): void {
    if (!this.canBuildInventory) return;

    this.loadingInventory = true;
    this.inventoryError = null;

    const request: DesignSystemInventoryRequest = {
      system_name: this.systemName.trim(),
      source: this.source,
    };

    this.api.buildDesignSystemInventory(request).subscribe({
      next: (res) => {
        this.inventory = res;
        this.components = res.components.map((name) => ({
          name,
          hasContract: false,
        }));
        this.loadingInventory = false;
      },
      error: (err) => {
        this.inventoryError = extractErrorDetail(err, 'Failed to build inventory');
        this.loadingInventory = false;
      },
    });
  }

  generateContract(component: ComponentEntry): void {
    if (!this.inventory) return;

    component.loading = true;

    const request: DesignSystemContractRequest = {
      system_name: this.inventory.system_name,
      component: component.name,
      platform: this.platform,
    };

    this.api.generateDesignSystemContract(request).subscribe({
      next: (res) => {
        component.contract = res;
        component.hasContract = true;
        component.loading = false;
      },
      error: (err) => {
        console.error('Failed to generate contract:', err);
        component.loading = false;
      },
    });
  }

  getContractRequirementsCount(contract: DesignSystemContractResponse): number {
    return Object.keys(contract.requirements || {}).length;
  }

  resetInventory(): void {
    this.inventory = null;
    this.components = [];
    this.systemName = '';
  }

  trackByComponentName(_index: number, component: ComponentEntry): string {
    return component.name;
  }
}
