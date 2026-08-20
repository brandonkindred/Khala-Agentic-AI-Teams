import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../environments/environment';
import type {
  BacklogTree,
  FeedbackItem,
  FeedbackLinkUpdate,
  GroomMethod,
  GroomRequest,
  GroomResult,
  Product,
  Release,
  ScoreUpdate,
  Sprint,
  SprintPlanResult,
  SprintWithStories,
  StatusUpdate,
  Story,
} from '../models/product-delivery.model';

/**
 * API client for the `product_delivery` team.
 *
 * Backs the Agent Console Backlog / Sprints / Groom / Feedback tabs.
 * Talks to the unified API at `${environment.productDeliveryApiUrl}`.
 * Errors are not handled here; subscribers should surface
 * `err?.error?.detail` at the component layer (existing project
 * convention — see `AgentConsoleApiService`).
 */
@Injectable({ providedIn: 'root' })
export class ProductDeliveryService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.productDeliveryApiUrl;

  // -------------------------------------------------------------------------
  // Products
  // -------------------------------------------------------------------------

  listProducts(): Observable<Product[]> {
    return this.http.get<Product[]>(`${this.baseUrl}/products`);
  }

  getBacklog(productId: string): Observable<BacklogTree> {
    return this.http.get<BacklogTree>(
      `${this.baseUrl}/products/${encodeURIComponent(productId)}/backlog`,
    );
  }

  // -------------------------------------------------------------------------
  // Story edits (Backlog drawer)
  // -------------------------------------------------------------------------

  patchStoryStatus(
    storyId: string,
    body: StatusUpdate,
  ): Observable<{ ok: boolean; kind: string; id: string; status: string }> {
    return this.http.patch<{ ok: boolean; kind: string; id: string; status: string }>(
      `${this.baseUrl}/story/${encodeURIComponent(storyId)}/status`,
      body,
    );
  }

  patchStoryScores(
    storyId: string,
    body: ScoreUpdate,
  ): Observable<{ ok: boolean; kind: string; id: string }> {
    return this.http.patch<{ ok: boolean; kind: string; id: string }>(
      `${this.baseUrl}/story/${encodeURIComponent(storyId)}/scores`,
      body,
    );
  }

  // -------------------------------------------------------------------------
  // Grooming
  // -------------------------------------------------------------------------

  groom(productId: string, method: GroomMethod, persist: boolean): Observable<GroomResult> {
    const body: GroomRequest = { product_id: productId, method, persist };
    return this.http.post<GroomResult>(`${this.baseUrl}/groom`, body);
  }

  // -------------------------------------------------------------------------
  // Sprints
  // -------------------------------------------------------------------------

  /** List every sprint under a product, newest first. */
  listSprints(productId: string): Observable<Sprint[]> {
    let params = new HttpParams();
    params = params.set('product_id', productId);
    return this.http.get<Sprint[]>(`${this.baseUrl}/sprints`, { params });
  }

  getSprint(sprintId: string): Observable<SprintWithStories> {
    return this.http.get<SprintWithStories>(
      `${this.baseUrl}/sprints/${encodeURIComponent(sprintId)}`,
    );
  }

  planSprint(sprintId: string, capacityPoints?: number | null): Observable<SprintPlanResult> {
    const body = capacityPoints !== undefined ? { capacity_points: capacityPoints } : null;
    return this.http.post<SprintPlanResult>(
      `${this.baseUrl}/sprints/${encodeURIComponent(sprintId)}/plan`,
      body,
    );
  }

  // -------------------------------------------------------------------------
  // Releases
  // -------------------------------------------------------------------------

  listReleases(productId: string): Observable<Release[]> {
    let params = new HttpParams();
    params = params.set('product_id', productId);
    return this.http.get<Release[]>(`${this.baseUrl}/releases`, { params });
  }

  // -------------------------------------------------------------------------
  // Feedback
  // -------------------------------------------------------------------------

  listFeedback(productId: string, status?: string | null): Observable<FeedbackItem[]> {
    let params = new HttpParams().set('product_id', productId);
    if (status) {
      params = params.set('status', status);
    }
    return this.http.get<FeedbackItem[]>(`${this.baseUrl}/feedback`, { params });
  }

  linkFeedback(feedbackId: string, storyId: string | null): Observable<FeedbackItem> {
    const body: FeedbackLinkUpdate = { linked_story_id: storyId };
    return this.http.patch<FeedbackItem>(
      `${this.baseUrl}/feedback/${encodeURIComponent(feedbackId)}/link`,
      body,
    );
  }

  // -------------------------------------------------------------------------
  // Helpers used by tabs
  // -------------------------------------------------------------------------

  /** Flatten a `BacklogTree` to a list of stories (used by feedback-link picker). */
  static flattenStories(tree: BacklogTree | null): Story[] {
    if (!tree) return [];
    const out: Story[] = [];
    for (const i of tree.initiatives) {
      for (const e of i.epics) {
        for (const s of e.stories) {
          out.push(s);
        }
      }
    }
    return out;
  }
}
