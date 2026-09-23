import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { ApiError } from './api-error';
import { SubmissionApi } from './submission-api';

const REQUEST = { name: 'Daniyar', email: 'daniyar-swe-demo-0923@mailinator.com' };
const TRACKING_ID = '019a0cac-19d1-7715-8846-1258380249ff';

describe('SubmissionApi', () => {
  let api: SubmissionApi;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    api = TestBed.inject(SubmissionApi);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  async function failWith(
    status: number,
    body: object | string | null,
    headers: Record<string, string> = {},
  ): Promise<ApiError> {
    const pending = new Promise<ApiError>((resolve, reject) => {
      api.createSubmission(REQUEST).subscribe({
        next: () => reject(new Error('expected the call to fail')),
        error: (error: unknown) =>
          error instanceof ApiError ? resolve(error) : reject(new Error('not an ApiError')),
      });
    });
    http.expectOne(() => true).flush(body, { status, statusText: 'error', headers });
    return pending;
  }

  it('posts the submission to the versioned API endpoint', () => {
    api.createSubmission(REQUEST).subscribe();

    const request = http.expectOne((req) => req.url.endsWith('/api/v1/submissions'));
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toEqual(REQUEST);
    request.flush({
      detail: 'Confirmation email accepted for delivery.',
      tracking_id: TRACKING_ID,
    });
  });

  it('resolves with the message and the tracking id', async () => {
    const created = {
      detail: 'Confirmation email accepted for delivery.',
      tracking_id: TRACKING_ID,
    };
    const body = await new Promise((resolve) => {
      api.createSubmission(REQUEST).subscribe(resolve);
      http.expectOne(() => true).flush(created);
    });

    expect(body).toEqual(created);
  });

  describe('getDeliveryStatus', () => {
    function readStatus(trackingId = TRACKING_ID): Promise<unknown> {
      return new Promise((resolve) => {
        api.getDeliveryStatus(trackingId).subscribe({ next: resolve, error: resolve });
      });
    }

    it('reads the stored status from the versioned status endpoint', async () => {
      const pending = readStatus();
      const request = http.expectOne((req) =>
        req.url.endsWith(`/api/v1/submissions/${TRACKING_ID}/status`),
      );
      expect(request.request.method).toBe('GET');
      request.flush({ status: 'queued' });

      expect(await pending).toBe('queued');
    });

    it('encodes the tracking id so it cannot change the path', () => {
      void readStatus('../x?y');

      http
        .expectOne((req) => req.url.endsWith('/api/v1/submissions/..%2Fx%3Fy/status'))
        .flush({
          status: 'queued',
        });
    });

    it('rejects a status value it does not know', async () => {
      const pending = readStatus();
      http.expectOne(() => true).flush({ status: 'teleported' });

      const error = await pending;
      expect(error).toBeInstanceOf(ApiError);
      expect((error as ApiError).message).toContain('does not understand');
    });

    it('surfaces the backend message for an unknown tracking id', async () => {
      const pending = readStatus();
      http
        .expectOne(() => true)
        .flush(
          { detail: 'No submission matches this tracking ID.' },
          { status: 404, statusText: 'Not Found' },
        );

      const error = await pending;
      expect(error).toBeInstanceOf(ApiError);
      expect((error as ApiError).status).toBe(404);
      expect((error as ApiError).message).toBe('No submission matches this tracking ID.');
    });
  });

  it('marks a 429 as rate limited, keeps the wait and shows the backend message', async () => {
    const detail = 'Too many confirmation requests. Try again in 42 seconds.';
    const error = await failWith(429, { detail }, { 'Retry-After': '42' });

    expect(error.isRateLimited).toBe(true);
    expect(error.retryAfterSeconds).toBe(42);
    expect(error.message).toBe(detail);
  });

  it('ignores an unusable Retry-After and falls back to a generic rate-limit message', async () => {
    const error = await failWith(429, 'Too Many Requests', { 'Retry-After': 'soon' });

    expect(error.retryAfterSeconds).toBeNull();
    expect(error.message).toContain('Too many confirmation requests');
  });

  it('surfaces the backend detail message for expected failures', async () => {
    const error = await failWith(502, { detail: 'Mailjet did not accept the email.' });

    expect(error.status).toBe(502);
    expect(error.message).toBe('Mailjet did not accept the email.');
  });

  it('uses a generic message when the backend gives no readable detail', async () => {
    const error = await failWith(500, 'Internal Server Error');

    expect(error.message).toBe('Something went wrong on the server. Please try again later.');
  });

  it('explains an unreachable server', async () => {
    const error = await failWith(0, null);

    expect(error.message).toContain('Could not reach the server');
  });
});
