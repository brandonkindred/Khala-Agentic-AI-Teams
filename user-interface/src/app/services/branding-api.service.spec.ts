import { TestBed } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { BrandingApiService } from './branding-api.service';
import { environment } from '../../environments/environment';

describe('BrandingApiService', () => {
  let service: BrandingApiService;
  let httpMock: HttpTestingController;
  const baseUrl = environment.brandingApiUrl;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [BrandingApiService],
    });
    service = TestBed.inject(BrandingApiService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
  });

  it('should call POST /branding/sessions', () => {
    service
      .createSession({
        company_name: 'Acme',
        company_description: 'Brand strategy for SMB fintech',
        target_audience: 'Founders',
      })
      .subscribe((res) => expect(res.session_id).toBe('s1'));

    const req = httpMock.expectOne(`${baseUrl}/branding/sessions`);
    expect(req.request.method).toBe('POST');
    req.flush({
      session_id: 's1',
      status: 'awaiting_user_answers',
      mission: {},
      latest_output: { status: 'needs_human_decision', mission_summary: '', brand_guidelines: [], writing_guidelines: { voice_principles: [] } },
      open_questions: [],
      answered_questions: [],
    });
  });

  it('should call POST question answer endpoint', () => {
    service.answerQuestion('s1', 'q1', 'clarity, trust').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/branding/sessions/s1/questions/q1/answer`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body.answer).toBe('clarity, trust');
    req.flush({
      session_id: 's1',
      status: 'ready_for_rollout',
      mission: {},
      latest_output: { status: 'ready_for_rollout', mission_summary: '', brand_guidelines: [], writing_guidelines: { voice_principles: [] } },
      open_questions: [],
      answered_questions: [],
    });
  });
});
