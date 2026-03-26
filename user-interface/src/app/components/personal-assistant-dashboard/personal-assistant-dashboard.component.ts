import { Component, inject, OnInit } from '@angular/core';
import { MatTabsModule } from '@angular/material/tabs';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { PersonalAssistantApiService } from '../../services/personal-assistant-api.service';
import { TeamAssistantChatComponent } from '../team-assistant-chat/team-assistant-chat.component';
import { LoadingSpinnerComponent } from '../../shared/loading-spinner/loading-spinner.component';
import { ErrorMessageComponent } from '../../shared/error-message/error-message.component';
import { HealthIndicatorComponent } from '../health-indicator/health-indicator.component';
import { PaChatComponent } from '../pa-chat/pa-chat.component';
import { PaProfileComponent } from '../pa-profile/pa-profile.component';
import { PaTasksComponent } from '../pa-tasks/pa-tasks.component';
import { PaCalendarComponent } from '../pa-calendar/pa-calendar.component';
import { PaDealsComponent } from '../pa-deals/pa-deals.component';
import { PaReservationsComponent } from '../pa-reservations/pa-reservations.component';
import { PaDocumentsComponent } from '../pa-documents/pa-documents.component';

/**
 * Personal Assistant Dashboard - main container with tabbed interface.
 */
@Component({
  selector: 'app-personal-assistant-dashboard',
  standalone: true,
  imports: [
    MatTabsModule,
    MatIconModule,
    MatButtonModule,
    LoadingSpinnerComponent,
    ErrorMessageComponent,
    HealthIndicatorComponent,
    PaChatComponent,
    PaProfileComponent,
    PaTasksComponent,
    PaCalendarComponent,
    PaDealsComponent,
    PaReservationsComponent,
    PaDocumentsComponent,
    TeamAssistantChatComponent,
  ],
  templateUrl: './personal-assistant-dashboard.component.html',
  styleUrl: './personal-assistant-dashboard.component.scss',
})
export class PersonalAssistantDashboardComponent implements OnInit {
  private readonly api = inject(PersonalAssistantApiService);

  mode: 'chat' | 'form' = 'chat';
  userId = 'default';
  loading = false;
  error: string | null = null;

  healthCheck = (): ReturnType<PersonalAssistantApiService['health']> => this.api.health();

  ngOnInit(): void {
    const storedUserId = localStorage.getItem('pa_user_id');
    if (storedUserId) {
      this.userId = storedUserId;
    }
  }

  onUserIdChange(newUserId: string): void {
    this.userId = newUserId;
    localStorage.setItem('pa_user_id', newUserId);
  }
}
