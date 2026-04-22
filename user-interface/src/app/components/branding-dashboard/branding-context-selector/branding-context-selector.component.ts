import { Component, EventEmitter, Input, Output } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatDividerModule } from '@angular/material/divider';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { MatMenuModule } from '@angular/material/menu';
import { MatSelectModule } from '@angular/material/select';
import type { Brand, Client } from '../../../models';

@Component({
  selector: 'app-branding-context-selector',
  standalone: true,
  imports: [
    FormsModule,
    MatButtonModule,
    MatDividerModule,
    MatFormFieldModule,
    MatIconModule,
    MatInputModule,
    MatMenuModule,
    MatSelectModule,
  ],
  templateUrl: './branding-context-selector.component.html',
  styleUrl: './branding-context-selector.component.scss',
})
export class BrandingContextSelectorComponent {
  @Input() clients: Client[] = [];
  @Input() selectedClient: Client | null = null;
  @Input() brands: Brand[] = [];
  @Input() selectedBrand: Brand | null = null;

  @Output() clientChange = new EventEmitter<Client>();
  @Output() brandChange = new EventEmitter<Brand>();
  @Output() newBrandRequest = new EventEmitter<void>();
  @Output() reloadClients = new EventEmitter<void>();
  @Output() addClient = new EventEmitter<string>();

  newWorkspaceName = '';

  onClientSelect(id: string): void {
    const match = this.clients.find((c) => c.id === id);
    if (match && match.id !== this.selectedClient?.id) this.clientChange.emit(match);
  }

  onBrandSelect(id: string): void {
    const match = this.brands.find((b) => b.id === id);
    if (match && match.id !== this.selectedBrand?.id) this.brandChange.emit(match);
  }

  submitNewWorkspace(): void {
    const name = this.newWorkspaceName.trim();
    if (!name) return;
    this.addClient.emit(name);
    this.newWorkspaceName = '';
  }
}
