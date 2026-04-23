import { Component, Input, Output, EventEmitter } from '@angular/core';
import { trigger, transition, style, animate } from '@angular/animations';
import { MatCardModule } from '@angular/material/card';
import { MatExpansionModule } from '@angular/material/expansion';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { MatChipsModule } from '@angular/material/chips';
import type {
  BrandingMissionSnapshot,
  BrandingTeamOutput,
  BrandPhase,
  ColorPalette,
} from '../../models';

type PhaseRenderStatus = 'not_started' | 'in_progress' | 'completed';

interface PhaseSpec {
  phase: BrandPhase;
  label: string;
  icon: string;
}

/** Pipeline order mirrors backend `PHASE_ORDER` in graphs/shared.py. */
const PHASES: readonly PhaseSpec[] = [
  { phase: 'strategic_core', label: 'Strategic Core', icon: 'hub' },
  { phase: 'narrative_messaging', label: 'Narrative', icon: 'edit_note' },
  { phase: 'visual_identity', label: 'Visual', icon: 'palette' },
  { phase: 'channel_activation', label: 'Channel', icon: 'campaign' },
  { phase: 'governance', label: 'Governance', icon: 'verified' },
];

const STATUS_LABELS: Record<PhaseRenderStatus, string> = {
  completed: 'Completed',
  in_progress: 'In progress',
  not_started: 'Not started',
};

@Component({
  selector: 'app-brand-preview',
  standalone: true,
  imports: [
    MatCardModule,
    MatExpansionModule,
    MatIconModule,
    MatButtonModule,
    MatChipsModule,
  ],
  templateUrl: './brand-preview.component.html',
  styleUrl: './brand-preview.component.scss',
  animations: [
    trigger('sectionEnter', [
      transition(':enter', [
        style({ opacity: 0, transform: 'translateY(8px)' }),
        animate('300ms cubic-bezier(0.23, 1, 0.32, 1)', style({ opacity: 1, transform: 'translateY(0)' })),
      ]),
    ]),
  ],
})
export class BrandPreviewComponent {
  @Input() mission: BrandingMissionSnapshot | null = null;
  @Input() latestOutput: BrandingTeamOutput | null = null;
  @Output() saveAsBrand = new EventEmitter<void>();
  @Output() selectPalette = new EventEmitter<number>();

  readonly phases = PHASES;

  brandBookOpen = false;

  get hasOutput(): boolean {
    return this.latestOutput != null;
  }

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

  onSelectPalette(index: number): void {
    this.selectPalette.emit(index);
  }

  /**
   * Render status for a phase. Prefers the backend `phase_gates` signal; falls
   * back to `phaseHasOutput` when gates are absent (e.g. older fixtures).
   */
  phaseStatus(phase: BrandPhase): PhaseRenderStatus {
    const gate = this.latestOutput?.phase_gates?.find((g) => g.phase === phase);
    if (gate) {
      if (gate.status === 'approved') return 'completed';
      if (gate.status === 'in_progress' || gate.status === 'pending_review') return 'in_progress';
      return 'not_started';
    }
    return this.phaseHasOutput(phase) ? 'completed' : 'not_started';
  }

  phaseStatusLabel(phase: BrandPhase): string {
    return STATUS_LABELS[this.phaseStatus(phase)];
  }

  phaseHasOutput(phase: BrandPhase): boolean {
    const out = this.latestOutput;
    if (!out) return false;
    switch (phase) {
      case 'strategic_core':
        return !!out.codification || !!out.mission_summary;
      case 'narrative_messaging':
        return !!out.writing_guidelines;
      case 'visual_identity':
        return (out.mood_boards?.length ?? 0) > 0 || !!out.design_system || this.missionPalettes.length > 0;
      case 'channel_activation':
        return !!out.creative_refinement || !!out.design_asset_result;
      case 'governance':
        return (out.brand_guidelines?.length ?? 0) > 0 || (out.wiki_backlog?.length ?? 0) > 0;
      default:
        return false;
    }
  }

  openBrandBook(): void {
    if (this.latestOutput?.brand_book?.content) {
      this.brandBookOpen = true;
    }
  }

  closeBrandBook(): void {
    this.brandBookOpen = false;
  }

  downloadBrandBook(): void {
    const content = this.latestOutput?.brand_book?.content;
    if (!content) return;
    const blob = new Blob([content], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = 'brand-book.md';
    document.body.appendChild(anchor);
    anchor.click();
    document.body.removeChild(anchor);
    URL.revokeObjectURL(url);
  }
}
