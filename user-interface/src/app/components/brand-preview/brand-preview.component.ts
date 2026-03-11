import { Component, Input, Output, EventEmitter } from '@angular/core';
import { MatCardModule } from '@angular/material/card';
import { MatTabsModule } from '@angular/material/tabs';
import { MatExpansionModule } from '@angular/material/expansion';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import type { BrandingMissionSnapshot, BrandingTeamOutput } from '../../models';

@Component({
  selector: 'app-brand-preview',
  standalone: true,
  imports: [
    MatCardModule,
    MatTabsModule,
    MatExpansionModule,
    MatIconModule,
    MatButtonModule,
  ],
  templateUrl: './brand-preview.component.html',
  styleUrl: './brand-preview.component.scss',
})
export class BrandPreviewComponent {
  @Input() mission: BrandingMissionSnapshot | null = null;
  @Input() latestOutput: BrandingTeamOutput | null = null;
  @Output() saveToAgency = new EventEmitter<void>();

  get hasOutput(): boolean {
    return this.latestOutput != null;
  }

  get colorStories(): string[] {
    const out = this.latestOutput;
    if (!out?.mood_boards?.length) {
      return out?.design_system?.foundation_tokens ?? [];
    }
    const colors: string[] = [];
    for (const mb of out.mood_boards) {
      if (mb.color_story.length) {
        colors.push(...mb.color_story);
      }
    }
    return [...new Set(colors)];
  }

  /** Return a CSS color for swatch display; supports hex or leaves as-is for names. */
  parseColor(token: string): string {
    const t = (token || '').trim();
    if (/^#([0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})$/.test(t)) return t;
    if (/^rgb\(/.test(t) || /^rgba\(/.test(t)) return t;
    return 'var(--brand-swatch-fallback, #6b7280)';
  }
}
