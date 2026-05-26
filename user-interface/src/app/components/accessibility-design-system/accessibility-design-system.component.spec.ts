import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { AccessibilityApiService } from '../../services/accessibility-api.service';
import { AccessibilityDesignSystemComponent } from './accessibility-design-system.component';

describe('AccessibilityDesignSystemComponent', () => {
  let component: AccessibilityDesignSystemComponent;
  let fixture: ComponentFixture<AccessibilityDesignSystemComponent>;
  let apiSpy: {
    buildDesignSystemInventory: ReturnType<typeof vi.fn>;
    generateDesignSystemContract: ReturnType<typeof vi.fn>;
  };

  beforeEach(async () => {
    apiSpy = {
      buildDesignSystemInventory: vi
        .fn()
        .mockReturnValue(of({ system_name: 'SDS', components: ['Button', 'Input'] })),
      generateDesignSystemContract: vi
        .fn()
        .mockReturnValue(of({ requirements: { keyboard: 'foo', focus: 'bar' } })),
    };
    await TestBed.configureTestingModule({
      imports: [AccessibilityDesignSystemComponent, NoopAnimationsModule],
      providers: [{ provide: AccessibilityApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(AccessibilityDesignSystemComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('canBuildInventory false when empty', () => {
    expect(component.canBuildInventory).toBe(false);
    component.systemName = 'SDS';
    expect(component.canBuildInventory).toBe(true);
  });

  it('buildInventory skipped without name', () => {
    component.buildInventory();
    expect(apiSpy.buildDesignSystemInventory).not.toHaveBeenCalled();
  });

  it('buildInventory populates components on success', () => {
    component.systemName = 'SDS';
    component.buildInventory();
    expect(component.inventory?.components.length).toBe(2);
    expect(component.components.map((c) => c.name)).toEqual(['Button', 'Input']);
    expect(component.loadingInventory).toBe(false);
  });

  it('buildInventory error path sets inventoryError', () => {
    apiSpy.buildDesignSystemInventory.mockReturnValue(
      throwError(() => ({ error: { detail: 'oops' } })),
    );
    component.systemName = 'SDS';
    component.buildInventory();
    expect(component.inventoryError).toBe('oops');
    expect(component.loadingInventory).toBe(false);
  });

  it('generateContract skipped without inventory', () => {
    const entry = { name: 'Button', hasContract: false };
    component.generateContract(entry);
    expect(apiSpy.generateDesignSystemContract).not.toHaveBeenCalled();
  });

  it('generateContract sets contract on success', () => {
    component.inventory = { system_name: 'SDS', components: [] } as never;
    const entry = { name: 'Button', hasContract: false } as never;
    component.generateContract(entry);
    expect(apiSpy.generateDesignSystemContract).toHaveBeenCalled();
    expect((entry as { hasContract: boolean }).hasContract).toBe(true);
  });

  it('generateContract error path', () => {
    apiSpy.generateDesignSystemContract.mockReturnValue(throwError(() => ({})));
    component.inventory = { system_name: 'SDS', components: [] } as never;
    const entry = { name: 'Button', hasContract: false, loading: false };
    component.generateContract(entry);
    expect(entry.loading).toBe(false);
  });

  it('getContractRequirementsCount counts keys', () => {
    expect(component.getContractRequirementsCount({ requirements: { a: 1, b: 2 } } as never)).toBe(2);
    expect(component.getContractRequirementsCount({} as never)).toBe(0);
  });

  it('resetInventory clears state', () => {
    component.inventory = { system_name: 'SDS', components: [] } as never;
    component.components = [{ name: 'X', hasContract: false }];
    component.systemName = 'something';
    component.resetInventory();
    expect(component.inventory).toBeNull();
    expect(component.components).toEqual([]);
    expect(component.systemName).toBe('');
  });

  it('trackByComponentName returns name', () => {
    expect(component.trackByComponentName(0, { name: 'X', hasContract: false })).toBe('X');
  });
});
