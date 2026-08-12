import { ChangeDetectionStrategy, Component } from '@angular/core';
import { MatIconModule } from '@angular/material/icon';
import { MatTabsModule } from '@angular/material/tabs';
import { BacklogTabComponent } from '../agent-team-studio/agent-console/backlog-tab/backlog-tab.component';
import { SprintsTabComponent } from '../agent-team-studio/agent-console/sprints-tab/sprints-tab.component';
import { FeedbackTabComponent } from '../agent-team-studio/agent-console/feedback-tab/feedback-tab.component';

/**
 * First-class host page for Product Delivery — mounts the pre-existing
 * Backlog/Sprints/Feedback tab components (previously nested inside
 * Agent Console) under their own top-level `/product-delivery` route.
 */
@Component({
  selector: 'app-product-delivery-page',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [MatIconModule, MatTabsModule, BacklogTabComponent, SprintsTabComponent, FeedbackTabComponent],
  templateUrl: './product-delivery-page.component.html',
  styleUrl: './product-delivery-page.component.scss',
})
export class ProductDeliveryPageComponent {}
