import { Component } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterModule } from '@angular/router';
import { MatCardModule } from '@angular/material/card';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { INTEGRATIONS } from '../../models/integrations.model';

@Component({
  selector: 'app-integrations-page',
  standalone: true,
  imports: [CommonModule, RouterModule, MatCardModule, MatButtonModule, MatIconModule],
  templateUrl: './integrations-page.component.html',
  styleUrl: './integrations-page.component.scss',
})
export class IntegrationsPageComponent {
  readonly integrations = INTEGRATIONS;
}
