import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { SimpleChange } from '@angular/core';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { PersonalAssistantApiService } from '../../services/personal-assistant-api.service';
import { PaDealsComponent } from './pa-deals.component';

describe('PaDealsComponent', () => {
  let component: PaDealsComponent;
  let fixture: ComponentFixture<PaDealsComponent>;
  let apiSpy: {
    getWishlist: ReturnType<typeof vi.fn>;
    addToWishlist: ReturnType<typeof vi.fn>;
    removeFromWishlist: ReturnType<typeof vi.fn>;
    searchDeals: ReturnType<typeof vi.fn>;
  };

  beforeEach(async () => {
    apiSpy = {
      getWishlist: vi.fn().mockReturnValue(of([{ item_id: 'i1', description: 'X' }])),
      addToWishlist: vi.fn().mockReturnValue(of({ item_id: 'i2', description: 'Y' })),
      removeFromWishlist: vi.fn().mockReturnValue(of(undefined)),
      searchDeals: vi.fn().mockReturnValue(of({ deals: [] })),
    };
    await TestBed.configureTestingModule({
      imports: [PaDealsComponent, NoopAnimationsModule],
      providers: [{ provide: PersonalAssistantApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(PaDealsComponent);
    component = fixture.componentInstance;
    component.userId = 'u1';
    fixture.detectChanges();
  });

  it('creates and loads wishlist', () => {
    expect(component).toBeTruthy();
    expect(component.wishlist.length).toBe(1);
  });

  it('loadWishlist handles error', () => {
    apiSpy.getWishlist.mockReturnValue(throwError(() => new Error('boom')));
    (component as unknown as { loadWishlist: () => void }).loadWishlist();
    expect(component.wishlist).toEqual([]);
    expect(component.loading).toBe(false);
  });

  it('ngOnChanges reloads on userId change', () => {
    apiSpy.getWishlist.mockClear();
    component.ngOnChanges({ userId: new SimpleChange('u1', 'u2', false) });
    expect(apiSpy.getWishlist).toHaveBeenCalled();
  });

  it('ngOnChanges ignores first change', () => {
    apiSpy.getWishlist.mockClear();
    component.ngOnChanges({ userId: new SimpleChange(undefined, 'u1', true) });
    expect(apiSpy.getWishlist).not.toHaveBeenCalled();
  });

  it('onAddToWishlist does nothing if invalid', () => {
    component.wishlistForm.setValue({ description: '', targetPrice: '', category: '' });
    component.onAddToWishlist();
    expect(apiSpy.addToWishlist).not.toHaveBeenCalled();
  });

  it('onAddToWishlist does nothing while adding', () => {
    component.wishlistForm.setValue({ description: 'New gadget', targetPrice: '', category: '' });
    component.addingItem = true;
    component.onAddToWishlist();
    expect(apiSpy.addToWishlist).not.toHaveBeenCalled();
  });

  it('onAddToWishlist posts with parsed price + category', () => {
    component.wishlistForm.setValue({
      description: 'New gadget',
      targetPrice: '99.99',
      category: 'tech',
    });
    component.onAddToWishlist();
    expect(apiSpy.addToWishlist).toHaveBeenCalledWith('u1', {
      description: 'New gadget',
      target_price: 99.99,
      category: 'tech',
    });
    expect(component.addingItem).toBe(false);
  });

  it('onAddToWishlist omits price when blank', () => {
    component.wishlistForm.setValue({ description: 'X gadget', targetPrice: '', category: '' });
    component.onAddToWishlist();
    expect(apiSpy.addToWishlist).toHaveBeenCalledWith('u1', {
      description: 'X gadget',
      target_price: undefined,
      category: undefined,
    });
  });

  it('onAddToWishlist error path', () => {
    apiSpy.addToWishlist.mockReturnValue(throwError(() => ({ error: { detail: 'oops' } })));
    component.wishlistForm.setValue({ description: 'X gadget', targetPrice: '', category: '' });
    component.onAddToWishlist();
    expect(component.addingItem).toBe(false);
  });

  it('onRemoveFromWishlist removes locally', () => {
    component.wishlist = [{ item_id: 'i1', description: 'X' } as never, { item_id: 'i2', description: 'Y' } as never];
    component.onRemoveFromWishlist({ item_id: 'i1' } as never);
    expect(component.wishlist.length).toBe(1);
    expect(component.wishlist[0].item_id).toBe('i2');
  });

  it('onRemoveFromWishlist error path', () => {
    apiSpy.removeFromWishlist.mockReturnValue(throwError(() => ({})));
    component.onRemoveFromWishlist({ item_id: 'i1' } as never);
  });

  it('onSearchDeals does nothing while searching', () => {
    component.searching = true;
    component.onSearchDeals();
    expect(apiSpy.searchDeals).not.toHaveBeenCalled();
  });

  it('onSearchDeals empty query => undefined', () => {
    component.searchForm.setValue({ query: '' });
    component.onSearchDeals();
    expect(apiSpy.searchDeals).toHaveBeenCalledWith('u1', { query: undefined });
  });

  it('onSearchDeals with query', () => {
    component.searchForm.setValue({ query: ' gadgets ' });
    apiSpy.searchDeals.mockReturnValue(of({ deals: [{ id: 'd1' }] }));
    component.onSearchDeals();
    expect(apiSpy.searchDeals).toHaveBeenCalledWith('u1', { query: 'gadgets' });
    expect(component.deals.length).toBe(1);
  });

  it('onSearchDeals empty results path', () => {
    apiSpy.searchDeals.mockReturnValue(of({ deals: [] }));
    component.onSearchDeals();
    expect(component.searching).toBe(false);
  });

  it('onSearchDeals error path', () => {
    apiSpy.searchDeals.mockReturnValue(throwError(() => ({})));
    component.onSearchDeals();
    expect(component.searching).toBe(false);
  });

  it('formatPrice/formatDiscount', () => {
    expect(component.formatPrice()).toBe('');
    expect(component.formatPrice(0)).toBe('');
    expect(component.formatPrice(9.5)).toBe('$9.50');
    expect(component.formatDiscount()).toBe('');
    expect(component.formatDiscount(0)).toBe('');
    expect(component.formatDiscount(25.4)).toBe('25% off');
  });
});
