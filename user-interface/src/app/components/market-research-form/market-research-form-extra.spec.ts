import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi, beforeEach } from 'vitest';
import { MarketResearchFormComponent } from './market-research-form.component';
import type { RunMarketResearchRequest } from '../../models';

describe('MarketResearchFormComponent (extra coverage)', () => {
  let component: MarketResearchFormComponent;
  let fixture: ComponentFixture<MarketResearchFormComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [MarketResearchFormComponent, NoopAnimationsModule],
    }).compileComponents();
    fixture = TestBed.createComponent(MarketResearchFormComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('onSubmit skips when form invalid', () => {
    const spy = vi.fn();
    component.submitRequest.subscribe(spy);
    component.onSubmit();
    expect(spy).not.toHaveBeenCalled();
  });

  it('onSubmit emits parsed payload', () => {
    let payload: RunMarketResearchRequest | undefined;
    component.submitRequest.subscribe((v) => { payload = v; });
    component.form.setValue({
      product_concept: 'AI app',
      target_users: 'devs',
      business_goal: 'grow',
      topology: 'split',
      transcript_folder_path: '/path',
      transcripts: ' a \nb\n\nc ',
      human_approved: true,
      human_feedback: 'thanks',
    });
    component.onSubmit();
    expect(payload?.product_concept).toBe('AI app');
    expect(payload?.topology).toBe('split');
    expect(payload?.transcript_folder_path).toBe('/path');
    expect(payload?.transcripts).toEqual(['a', 'b', 'c']);
    expect(payload?.human_approved).toBe(true);
  });

  it('onSubmit handles empty transcripts and folder', () => {
    let payload: RunMarketResearchRequest | undefined;
    component.submitRequest.subscribe((v) => { payload = v; });
    component.form.setValue({
      product_concept: 'AI app',
      target_users: 'devs',
      business_goal: 'grow',
      topology: 'unified',
      transcript_folder_path: '',
      transcripts: '',
      human_approved: false,
      human_feedback: '',
    });
    component.onSubmit();
    expect(payload?.transcripts).toEqual([]);
    expect(payload?.transcript_folder_path).toBeUndefined();
  });

  it('has expected topology options', () => {
    expect(component.topologyOptions.length).toBe(2);
    expect(component.topologyOptions.map((o) => o.value)).toEqual(['unified', 'split']);
  });
});
