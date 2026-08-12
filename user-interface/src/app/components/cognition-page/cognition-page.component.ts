import { ChangeDetectionStrategy, Component } from '@angular/core';
import { CognitionTabComponent } from './cognition-tab/cognition-tab.component';

/**
 * First-class host page for Cognition — mounts the pre-existing
 * Cognition tab (previously nested inside Agent Console) under its own
 * top-level `/cognition` route.
 *
 * Preconditions: none (no inputs).
 * Postconditions: the view contains `app-cognition-tab`.
 * Invariants: this host does not fetch data or add chrome; CognitionTabComponent
 * owns the operator surface.
 */
@Component({
  selector: 'app-cognition-page',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CognitionTabComponent],
  templateUrl: './cognition-page.component.html',
})
export class CognitionPageComponent {}
