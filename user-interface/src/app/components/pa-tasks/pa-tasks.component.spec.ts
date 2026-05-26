import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { SimpleChange } from '@angular/core';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { PersonalAssistantApiService } from '../../services/personal-assistant-api.service';
import { PaTasksComponent } from './pa-tasks.component';

describe('PaTasksComponent', () => {
  let component: PaTasksComponent;
  let fixture: ComponentFixture<PaTasksComponent>;
  let apiSpy: {
    getTasks: ReturnType<typeof vi.fn>;
    addTasksFromText: ReturnType<typeof vi.fn>;
    updateTaskItem: ReturnType<typeof vi.fn>;
  };

  beforeEach(async () => {
    apiSpy = {
      getTasks: vi.fn().mockReturnValue(of([{ list_id: 'l1', name: 'Inbox', items: [] }])),
      addTasksFromText: vi.fn().mockReturnValue(of({ success: true, added_items: [{}, {}] })),
      updateTaskItem: vi.fn().mockReturnValue(of({})),
    };
    await TestBed.configureTestingModule({
      imports: [PaTasksComponent, NoopAnimationsModule],
      providers: [{ provide: PersonalAssistantApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(PaTasksComponent);
    component = fixture.componentInstance;
    component.userId = 'u1';
    fixture.detectChanges();
  });

  it('creates and loads tasks on init', () => {
    expect(component).toBeTruthy();
    expect(apiSpy.getTasks).toHaveBeenCalledWith('u1');
    expect(component.taskLists.length).toBe(1);
  });

  it('loadTasks handles errors', () => {
    apiSpy.getTasks.mockReturnValue(throwError(() => new Error('boom')));
    (component as unknown as { loadTasks: () => void }).loadTasks();
    expect(component.taskLists).toEqual([]);
    expect(component.loading).toBe(false);
  });

  it('ngOnChanges reloads on userId change', () => {
    apiSpy.getTasks.mockClear();
    component.ngOnChanges({ userId: new SimpleChange('u1', 'u2', false) });
    expect(apiSpy.getTasks).toHaveBeenCalledWith('u1');
  });

  it('ngOnChanges ignores first change', () => {
    apiSpy.getTasks.mockClear();
    component.ngOnChanges({ userId: new SimpleChange(undefined, 'u1', true) });
    expect(apiSpy.getTasks).not.toHaveBeenCalled();
  });

  it('onAddTasks does nothing when invalid', () => {
    component.form.setValue({ taskText: 'ab' });
    component.onAddTasks();
    expect(apiSpy.addTasksFromText).not.toHaveBeenCalled();
  });

  it('onAddTasks does nothing while adding', () => {
    component.form.setValue({ taskText: 'buy milk' });
    component.addingTasks = true;
    component.onAddTasks();
    expect(apiSpy.addTasksFromText).not.toHaveBeenCalled();
  });

  it('onAddTasks success reloads', () => {
    component.form.setValue({ taskText: 'buy milk' });
    apiSpy.getTasks.mockClear();
    component.onAddTasks();
    expect(apiSpy.addTasksFromText).toHaveBeenCalledWith('u1', { text: 'buy milk' });
    expect(apiSpy.getTasks).toHaveBeenCalled();
    expect(component.addingTasks).toBe(false);
  });

  it('onAddTasks failure path runs', () => {
    apiSpy.addTasksFromText.mockReturnValue(of({ success: false, message: 'nope' }));
    component.form.setValue({ taskText: 'buy milk' });
    component.onAddTasks();
    expect(component.addingTasks).toBe(false);
  });

  it('onAddTasks API error handled', () => {
    apiSpy.addTasksFromText.mockReturnValue(throwError(() => ({ error: { detail: 'oops' } })));
    component.form.setValue({ taskText: 'buy milk' });
    component.onAddTasks();
    expect(component.addingTasks).toBe(false);
  });

  it('onToggleItem toggles pending->completed', () => {
    const list = { list_id: 'l1', name: 'X', items: [] } as never;
    const item = { item_id: 'i1', status: 'pending' } as never;
    component.onToggleItem(list, item);
    expect(apiSpy.updateTaskItem).toHaveBeenCalledWith('u1', 'l1', 'i1', { status: 'completed' });
    expect((item as { status: string }).status).toBe('completed');
  });

  it('onToggleItem toggles completed->pending', () => {
    component.onToggleItem(
      { list_id: 'l1' } as never,
      { item_id: 'i1', status: 'completed' } as never,
    );
    expect(apiSpy.updateTaskItem).toHaveBeenCalledWith('u1', 'l1', 'i1', { status: 'pending' });
  });

  it('onToggleItem error path runs', () => {
    apiSpy.updateTaskItem.mockReturnValue(throwError(() => ({})));
    component.onToggleItem(
      { list_id: 'l1' } as never,
      { item_id: 'i1', status: 'pending' } as never,
    );
  });

  it('getPendingCount / getCompletedCount', () => {
    const list = {
      list_id: 'l1',
      name: 'X',
      items: [
        { status: 'completed' },
        { status: 'pending' },
        { status: 'pending' },
      ],
    } as never;
    expect(component.getPendingCount(list)).toBe(2);
    expect(component.getCompletedCount(list)).toBe(1);
  });

  it('getPriorityColor maps priority', () => {
    expect(component.getPriorityColor('high')).toBe('#f85149');
    expect(component.getPriorityColor('medium')).toBe('#d29922');
    expect(component.getPriorityColor('low')).toBe('#58a6ff');
    expect(component.getPriorityColor()).toBe('#8b949e');
  });
});
