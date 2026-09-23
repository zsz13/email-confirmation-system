import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable, catchError, map, throwError } from 'rxjs';

import { environment } from '../../../environments/environment';
import {
  DeliveryStatus,
  DeliveryStatusResponse,
  SubmissionRequest,
  SubmissionResponse,
  isDeliveryStatus,
} from '../models/submission';
import { ApiError, toApiError } from './api-error';

const UNEXPECTED_STATUS = 'The server returned a delivery status this page does not understand.';

/** Public API version this frontend is built against. */
export const API_PREFIX = '/api/v1';

@Injectable({ providedIn: 'root' })
export class SubmissionApi {
  private readonly http = inject(HttpClient);
  private readonly submissionsUrl = `${environment.apiBaseUrl}${API_PREFIX}/submissions`;

  /** Store a submission and trigger its confirmation email. Errors surface as ApiError. */
  createSubmission(request: SubmissionRequest): Observable<SubmissionResponse> {
    return this.http
      .post<SubmissionResponse>(this.submissionsUrl, request)
      .pipe(catchError((error: unknown) => throwError(() => toApiError(error))));
  }

  /**
   * The delivery state the backend has stored for one submission. A read of the database
   * only: the backend's own reconciler is what asks the email provider for updates.
   */
  getDeliveryStatus(trackingId: string): Observable<DeliveryStatus> {
    const url = `${this.submissionsUrl}/${encodeURIComponent(trackingId)}/status`;
    return this.http.get<DeliveryStatusResponse>(url).pipe(
      catchError((error: unknown) => throwError(() => toApiError(error))),
      map((body) => {
        const status: unknown = (body as Partial<DeliveryStatusResponse> | null)?.status;
        if (!isDeliveryStatus(status)) {
          throw new ApiError(UNEXPECTED_STATUS, 200);
        }
        return status;
      }),
    );
  }
}
