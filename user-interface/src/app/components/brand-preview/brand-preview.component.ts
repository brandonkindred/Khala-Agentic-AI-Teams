import { Component, Input, Output, EventEmitter } from '@angular/core';
import { MatCardModule } from '@angular/material/card';
import { MatTabsModule } from '@angular/material/tabs';
import { MatExpansionModule } from '@angular/material/expansion';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { MatChipsModule } from '@angular/material/chips';
import type { BrandingMissionSnapshot, BrandingTeamOutput, ColorPalette } from '../../models';

@Component({
  selector: 'app-brand-preview',
  standalone: true,
  imports: [
    MatCardModule,
    MatTabsModule,
    MatExpansionModule,
    MatIconModule,
    MatButtonModule,
    MatChipsModule,
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

  /** True when mission has at least basic info worth showing. */
  get hasMissionData(): boolean {
    const m = this.mission;
    if (!m) return false;
    return (
      (m.company_name !== 'TBD' && m.company_name !== '') ||
      (m.values?.length ?? 0) > 0 ||
      (m.color_inspiration?.length ?? 0) > 0 ||
      (m.color_palettes?.length ?? 0) > 0 ||
      !!m.visual_style ||
      !!m.typography_preference
    );
  }

  /** True when there is anything to display (mission or output). */
  get hasContent(): boolean {
    return this.hasOutput || this.hasMissionData;
  }

  get missionValues(): string[] {
    return this.mission?.values ?? [];
  }

  get missionDifferentiators(): string[] {
    return this.mission?.differentiators ?? [];
  }

  get missionColorInspiration(): string[] {
    return this.mission?.color_inspiration ?? [];
  }

  get missionPalettes(): ColorPalette[] {
    return this.mission?.color_palettes ?? [];
  }

  get selectedPaletteIndex(): number | null | undefined {
    return this.mission?.selected_palette_index;
  }

  get selectedPalette(): ColorPalette | null {
    const idx = this.selectedPaletteIndex;
    if (idx == null) return null;
    const palettes = this.missionPalettes;
    return idx >= 0 && idx < palettes.length ? palettes[idx] : null;
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

  isPaletteSelected(index: number): boolean {
    return this.selectedPaletteIndex === index;
  }
}
