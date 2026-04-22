import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { BrandingContextSelectorComponent } from './branding-context-selector.component';
import type { Brand, Client } from '../../../models';

const client = (id: string, name = `Workspace ${id}`): Client => ({
  id,
  name,
  created_at: '',
  updated_at: '',
});

const brand = (id: string, version = 1): Brand => ({
  id,
  client_id: 'w1',
  name: `Brand ${id}`,
  status: 'draft',
  mission: {} as Brand['mission'],
  version,
  history: [],
  created_at: '',
  updated_at: '',
});

describe('BrandingContextSelectorComponent', () => {
  let component: BrandingContextSelectorComponent;
  let fixture: ComponentFixture<BrandingContextSelectorComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [BrandingContextSelectorComponent, NoopAnimationsModule],
    }).compileComponents();

    fixture = TestBed.createComponent(BrandingContextSelectorComponent);
    component = fixture.componentInstance;
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('emits clientChange with the full Client when a new workspace id is selected', () => {
    const c1 = client('w1');
    const c2 = client('w2');
    component.clients = [c1, c2];
    component.selectedClient = c1;
    const spy = vi.fn();
    component.clientChange.subscribe(spy);

    component.onClientSelect('w2');

    expect(spy).toHaveBeenCalledWith(c2);
  });

  it('does not emit clientChange when the selected workspace is unchanged', () => {
    const c1 = client('w1');
    component.clients = [c1];
    component.selectedClient = c1;
    const spy = vi.fn();
    component.clientChange.subscribe(spy);

    component.onClientSelect('w1');

    expect(spy).not.toHaveBeenCalled();
  });

  it('emits brandChange with the full Brand when a new brand id is selected', () => {
    const b1 = brand('b1');
    const b2 = brand('b2', 3);
    component.brands = [b1, b2];
    component.selectedBrand = b1;
    const spy = vi.fn();
    component.brandChange.subscribe(spy);

    component.onBrandSelect('b2');

    expect(spy).toHaveBeenCalledWith(b2);
  });

  it('disables the brand select when there are no brands', () => {
    component.brands = [];
    fixture.detectChanges();
    const brandSelect = fixture.nativeElement.querySelector('.ctx-brand mat-select');
    expect(brandSelect?.getAttribute('aria-disabled')).toBe('true');
  });

  it('renders Version "—" when no brand selected and "v{n}" when one is', () => {
    component.selectedBrand = null;
    fixture.detectChanges();
    const empty = fixture.nativeElement.querySelector('.ctx-version__value');
    expect(empty.textContent.trim()).toBe('—');

    component.selectedBrand = brand('b1', 7);
    fixture.detectChanges();
    const filled = fixture.nativeElement.querySelector('.ctx-version__value');
    expect(filled.textContent.trim()).toBe('v7');
  });

  it('emits newBrandRequest when the New brand button is clicked', () => {
    fixture.detectChanges();
    const spy = vi.fn();
    component.newBrandRequest.subscribe(spy);
    const button: HTMLButtonElement | null = fixture.nativeElement.querySelector('.ctx-new-brand');
    button?.click();
    expect(spy).toHaveBeenCalled();
  });

  it('submitNewWorkspace emits trimmed name and clears the field', () => {
    component.newWorkspaceName = '  My new workspace  ';
    const spy = vi.fn();
    component.addClient.subscribe(spy);

    component.submitNewWorkspace();

    expect(spy).toHaveBeenCalledWith('My new workspace');
    expect(component.newWorkspaceName).toBe('');
  });

  it('submitNewWorkspace ignores empty input', () => {
    component.newWorkspaceName = '   ';
    const spy = vi.fn();
    component.addClient.subscribe(spy);

    component.submitNewWorkspace();

    expect(spy).not.toHaveBeenCalled();
  });
});
