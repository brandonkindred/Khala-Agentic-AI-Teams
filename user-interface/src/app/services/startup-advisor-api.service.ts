import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../environments/environment';
import type {
  StartupAdvisorConversationState,
  StartupAdvisorArtifact,
} from '../models';

@Injectable({ providedIn: 'root' })
export class StartupAdvisorApiService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.startupAdvisorApiUrl;

  /** GET /conversation — get or create the singleton conversation */
  getConversation(): Observable<StartupAdvisorConversationState> {
    return this.http.get<StartupAdvisorConversationState>(`${this.baseUrl}/conversation`);
  }

  /** POST /conversation/messages — send a message and get advisor response */
  sendMessage(message: string): Observable<StartupAdvisorConversationState> {
    return this.http.post<StartupAdvisorConversationState>(
      `${this.baseUrl}/conversation/messages`,
      { message }
    );
  }

  /** GET /conversation/artifacts — list all artifacts */
  getArtifacts(): Observable<StartupAdvisorArtifact[]> {
    return this.http.get<StartupAdvisorArtifact[]>(`${this.baseUrl}/conversation/artifacts`);
  }
}
