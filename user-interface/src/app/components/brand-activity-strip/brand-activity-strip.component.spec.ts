import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { BrandActivityStripComponent } from './brand-activity-strip.component';
import { BrandActivityService } from '../../services/brand-activity.service';

describe('BrandActivityStripComponent', () => {
  let fixture: ComponentFixture<BrandActivityStripComponent>;
  let component: BrandActivityStripComponent;
  let store: BrandActivityService;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [BrandActivityStripComponent, NoopAnimationsModule],
      providers: [BrandActivityService],
    }).compileComponents();

    fixture = TestBed.createComponent(BrandActivityStripComponent);
    component = fixture.componentInstance;
    store = TestBed.inject(BrandActivityService);
    fixture.componentRef.setInput('brandId', 'b1');
    fixture.detectChanges();
  });

  it('subscribes to the activity store for the given brand', () => {
    store.start('run', 'b1');
    store.start('research', 'b-other');
    fixture.detectChanges();
    expect(component.items().map((a) => a.brandId)).toEqual(['b1']);
  });

  it('label() describes running activities with phase and progress', () => {
    const activity = store.start('run', 'b1');
    store.update(activity.id, { status: 'running', phase: 'Visual Identity', progress: 60 });
    fixture.detectChanges();
    expect(component.label(component.items()[0])).toContain('Visual Identity');
    expect(component.label(component.items()[0])).toContain('60%');
  });

  it('isOpenable() is true only for completed activities', () => {
    const completed = store.start('run', 'b1');
    store.update(completed.id, { status: 'completed' });
    const failed = store.start('run', 'b1');
    store.update(failed.id, { status: 'failed' });
    fixture.detectChanges();
    expect(component.isOpenable(component.items().find((a) => a.status === 'completed')!)).toBe(true);
    expect(component.isOpenable(component.items().find((a) => a.status === 'failed')!)).toBe(false);
  });

  it('onOpen() emits only for completed activities', () => {
    const spy = vi.fn();
    component.open.subscribe(spy);
    const running = store.start('run', 'b1');
    store.update(running.id, { status: 'running' });
    fixture.detectChanges();
    component.onOpen(new Event('click'), component.items()[0]);
    expect(spy).not.toHaveBeenCalled();

    store.update(running.id, { status: 'completed' });
    fixture.detectChanges();
    component.onOpen(new Event('click'), component.items()[0]);
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it('onRetry() emits for failed activities', () => {
    const spy = vi.fn();
    component.retry.subscribe(spy);
    const failed = store.start('run', 'b1');
    store.update(failed.id, { status: 'failed', error: 'boom' });
    fixture.detectChanges();
    component.onRetry(new Event('click'), component.items()[0]);
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it('onDismiss() emits for any terminal activity', () => {
    const spy = vi.fn();
    component.dismiss.subscribe(spy);
    const done = store.start('run', 'b1');
    store.update(done.id, { status: 'completed' });
    fixture.detectChanges();
    component.onDismiss(new Event('click'), component.items()[0]);
    expect(spy).toHaveBeenCalledTimes(1);
  });
});
