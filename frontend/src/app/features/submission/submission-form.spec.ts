import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { ComponentFixture, TestBed } from '@angular/core/testing';

import {
  DELIVERY_STATUSES,
  DeliveryStatus,
  SubmissionResponse,
} from '../../core/models/submission';
import {
  DELIVERY_STATUS_VIEWS,
  STATUS_POLL_INTERVAL_MS,
  STATUS_POLL_WINDOW_MS,
  TERMINAL_DELIVERY_STATUSES,
} from './delivery-status';
import { SPAM_NOTICE, SubmissionForm } from './submission-form';

const RECIPIENT = 'daniyar-swe-demo-0923@mailinator.com';
const TRACKING_ID = '019a0cac-19d1-7715-8846-1258380249ff';
const STATUS_URL = `/api/v1/submissions/${TRACKING_ID}/status`;
const SUCCESS: SubmissionResponse = {
  detail: 'Confirmation email accepted for delivery.',
  tracking_id: TRACKING_ID,
};

describe('SubmissionForm', () => {
  let fixture: ComponentFixture<SubmissionForm>;
  let http: HttpTestingController;
  let root: HTMLElement;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [SubmissionForm],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();

    fixture = TestBed.createComponent(SubmissionForm);
    http = TestBed.inject(HttpTestingController);
    root = fixture.nativeElement as HTMLElement;
    await fixture.whenStable();
  });

  afterEach(() => http.verify());

  function typeInto(id: string, value: string): void {
    const input = root.querySelector<HTMLInputElement>(`#${id}`);
    if (!input) {
      throw new Error(`missing input #${id}`);
    }
    input.value = value;
    input.dispatchEvent(new Event('input'));
  }

  function submitForm(): void {
    root.querySelector('form')?.dispatchEvent(new Event('submit'));
    fixture.detectChanges();
  }

  it('shows validation messages and sends nothing when fields are invalid', () => {
    typeInto('email', 'not-an-email');
    submitForm();

    expect(root.querySelector('#name-error')?.textContent).toContain('Enter your name.');
    expect(root.querySelector('#email-error')?.textContent).toContain('valid email address');
    expect(document.activeElement?.id).toBe('name');
    http.expectNone(() => true);
  });

  it('rejects an address without a dotted domain, unlike the built-in email validator', () => {
    typeInto('name', 'Daniyar');
    typeInto('email', 'user@localhost');
    submitForm();

    expect(root.querySelector('#email-error')?.textContent).toContain('valid email address');
    http.expectNone(() => true);
  });

  it('does not flag a field that was only focused and left empty', () => {
    const name = root.querySelector<HTMLInputElement>('#name');
    name?.dispatchEvent(new Event('focus'));
    name?.dispatchEvent(new Event('blur'));
    fixture.detectChanges();

    expect(root.querySelector('#name-error')).toBeNull();
  });

  it('ignores a second submit while a request is in flight', () => {
    typeInto('name', 'Daniyar');
    typeInto('email', RECIPIENT);
    submitForm();
    submitForm();

    http.expectOne((req) => req.url.endsWith('/api/v1/submissions'));
    http.expectNone(() => true);
  });

  it('shows the submitted address on the receipt, since the API does not echo it', async () => {
    typeInto('name', '  Daniyar  ');
    typeInto('email', ` ${RECIPIENT} `);
    submitForm();

    const request = http.expectOne((req) => req.url.endsWith('/api/v1/submissions'));
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toEqual({ name: 'Daniyar', email: RECIPIENT });
    expect(root.querySelector<HTMLButtonElement>('button[type=submit]')?.disabled).toBe(true);

    request.flush(SUCCESS, { status: 201, statusText: 'Created' });
    await fixture.whenStable();

    const receipt = root.querySelector('.receipt');
    const text = receipt?.textContent?.replace(/\s+/g, ' ') ?? '';
    expect(root.querySelector('.receipt-title')?.textContent?.trim()).toBe(
      'Confirmation accepted for delivery',
    );
    expect(text).toContain(
      'Your confirmation email has been accepted by the email provider. Delivery may take a moment.',
    );
    expect(root.querySelector('.receipt-address')?.textContent?.trim()).toBe(RECIPIENT);
    expect(text).toContain(SPAM_NOTICE);
    expect(SPAM_NOTICE).toMatch(/^If you don’t see it shortly, check your Spam or Junk folder\./);
    // A 201 is acceptance by the provider, not delivery: the page must not claim more.
    expect(text).not.toMatch(/delivered|inbox|successfully|guarantee/i);
    expect(root.querySelector('[aria-live=polite]')?.textContent).toContain(
      `Confirmation email to ${RECIPIENT} accepted for delivery.`,
    );
    expect(root.querySelector('form')).toBeNull();
  });

  it('lets the person send another confirmation from the success state', async () => {
    typeInto('name', 'Daniyar');
    typeInto('email', RECIPIENT);
    submitForm();
    http
      .expectOne((req) => req.url.endsWith('/api/v1/submissions'))
      .flush(SUCCESS, { status: 201, statusText: 'Created' });
    await fixture.whenStable();

    root.querySelector<HTMLButtonElement>('[data-action=start-over]')?.click();
    await fixture.whenStable();

    expect(root.querySelector('.receipt')).toBeNull();
    expect(root.querySelector<HTMLInputElement>('#email')?.value).toBe('');
    expect(root.querySelector('#name-error')).toBeNull();
  });

  it('explains a rate limit with the wait time and keeps the form usable', async () => {
    typeInto('name', 'Daniyar');
    typeInto('email', RECIPIENT);
    submitForm();

    http
      .expectOne((req) => req.url.endsWith('/api/v1/submissions'))
      .flush(
        { detail: 'Too many confirmation requests. Try again in 45 seconds.' },
        { status: 429, statusText: 'Too Many Requests', headers: { 'Retry-After': '45' } },
      );
    await fixture.whenStable();

    const alert = root.querySelector('[role=alert]');
    expect(alert?.textContent).toContain('Too many requests');
    expect(alert?.textContent).toContain('Try again in 45 seconds');
    expect(root.querySelector<HTMLButtonElement>('button[type=submit]')?.disabled).toBe(false);
    expect(root.querySelector<HTMLInputElement>('#email')?.value).toBe(RECIPIENT);
  });

  it('shows the API message and re-enables the form when sending fails', async () => {
    typeInto('name', 'Daniyar');
    typeInto('email', RECIPIENT);
    submitForm();

    const detail = 'Your submission was saved, but the confirmation email could not be sent.';
    http
      .expectOne((req) => req.url.endsWith('/api/v1/submissions'))
      .flush({ detail }, { status: 502, statusText: 'Bad Gateway' });
    await fixture.whenStable();

    expect(root.querySelector('[role=alert]')?.textContent).toContain(detail);
    expect(root.querySelector<HTMLButtonElement>('button[type=submit]')?.disabled).toBe(false);
    expect(root.querySelector<HTMLInputElement>('#email')?.value).toBe(RECIPIENT);
  });
  describe('delivery status tracking', () => {
    beforeEach(() => {
      vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] });
    });

    afterEach(() => {
      vi.useRealTimers();
    });

    const isStatusRequest = (req: { url: string }) => req.url.endsWith(STATUS_URL);

    async function settle(): Promise<void> {
      await vi.advanceTimersByTimeAsync(0);
      fixture.detectChanges();
    }

    async function advance(ms: number): Promise<void> {
      await vi.advanceTimersByTimeAsync(ms);
      fixture.detectChanges();
    }

    async function submitSuccessfully(): Promise<void> {
      typeInto('name', 'Daniyar');
      typeInto('email', RECIPIENT);
      submitForm();
      http
        .expectOne((req) => req.url.endsWith('/api/v1/submissions'))
        .flush(SUCCESS, { status: 201, statusText: 'Created' });
      await settle();
    }

    async function answerPoll(status: DeliveryStatus): Promise<void> {
      await advance(STATUS_POLL_INTERVAL_MS);
      http.expectOne(isStatusRequest).flush({ status });
      await settle();
    }

    function badge(): string {
      return root.querySelector('.delivery-badge')?.textContent?.trim() ?? '';
    }

    function deliveryText(): string {
      return root.querySelector('.delivery')?.textContent?.replace(/\s+/g, ' ') ?? '';
    }

    function checkButton(): HTMLButtonElement {
      const button = root.querySelector<HTMLButtonElement>('[data-action=check-status]');
      if (!button) {
        throw new Error('missing Check delivery status button');
      }
      return button;
    }

    it('keeps the tracking id and starts reading the status automatically', async () => {
      await submitSuccessfully();

      expect(badge()).toBe('Accepted for delivery');
      expect(deliveryText()).toContain('Checking automatically every 3 seconds.');
      // The first read waits one interval: a 201 already means "accepted".
      http.expectNone(isStatusRequest);

      await advance(2_999);
      http.expectNone(isStatusRequest);
      await advance(1);
      const request = http.expectOne(isStatusRequest);
      expect(request.request.method).toBe('GET');
      expect(request.request.url).toContain(TRACKING_ID);
      request.flush({ status: 'queued' });
      await settle();

      expect(badge()).toBe('Queued by email provider');

      // Every 3 seconds after the previous answer, not before.
      await advance(2_999);
      http.expectNone(isStatusRequest);
      await advance(1);
      http.expectOne(isStatusRequest).flush({ status: 'queued' });
      await settle();
    });

    it('updates the badge and the live region as the stored status moves on', async () => {
      await submitSuccessfully();

      await answerPoll('queued');
      expect(badge()).toBe('Queued by email provider');
      await answerPoll('retrying');
      expect(badge()).toBe('Delivery is being retried');
      expect(root.querySelector('.delivery-badge')?.getAttribute('data-tone')).toBe('warning');
      await answerPoll('delivered');

      expect(badge()).toBe('Delivered to recipient mail server');
      expect(root.querySelector('[aria-live=polite]')?.textContent).toContain(
        'Delivery status: Delivered to recipient mail server.',
      );
    });

    it.each([...TERMINAL_DELIVERY_STATUSES])(
      'stops polling once the status is final (%s)',
      async (status) => {
        await submitSuccessfully();
        await answerPoll('queued');
        await answerPoll(status);

        await advance(STATUS_POLL_WINDOW_MS);

        http.expectNone(isStatusRequest);
        expect(deliveryText()).not.toContain('Checking automatically');
        expect(deliveryText()).not.toContain('still being processed');
      },
    );

    it('stops polling when the window closes and says delivery is still in progress', async () => {
      await submitSuccessfully();

      let reads = 0;
      for (let elapsed = 0; elapsed < STATUS_POLL_WINDOW_MS; elapsed += STATUS_POLL_INTERVAL_MS) {
        await answerPoll('queued');
        reads += 1;
      }
      await advance(10 * STATUS_POLL_WINDOW_MS);

      expect(reads).toBe(STATUS_POLL_WINDOW_MS / STATUS_POLL_INTERVAL_MS);
      http.expectNone(isStatusRequest);
      expect(badge()).toBe('Queued by email provider');
      expect(deliveryText()).toContain('Delivery is still being processed.');
      expect(deliveryText()).not.toContain('Checking automatically');
    });

    it('never has two status requests in flight at once', async () => {
      await submitSuccessfully();
      await advance(STATUS_POLL_INTERVAL_MS);
      const slow = http.expectOne(isStatusRequest);

      // The answer is slow: time passes and the button is pressed, but nothing new is sent.
      await advance(6 * STATUS_POLL_INTERVAL_MS);
      checkButton().click();
      await settle();
      expect(http.match(isStatusRequest)).toEqual([]);
      expect(checkButton().disabled).toBe(true);

      slow.flush({ status: 'queued' });
      await settle();
      // The next read is queued only once the previous one answered.
      await advance(STATUS_POLL_INTERVAL_MS - 1);
      http.expectNone(isStatusRequest);
      await advance(1);
      http.expectOne(isStatusRequest).flush({ status: 'queued' });
      await settle();
    });

    it('checks on demand, with a loading state, even after automatic polling stopped', async () => {
      await submitSuccessfully();
      for (let elapsed = 0; elapsed < STATUS_POLL_WINDOW_MS; elapsed += STATUS_POLL_INTERVAL_MS) {
        await answerPoll('accepted');
      }
      expect(deliveryText()).toContain('Delivery is still being processed.');

      checkButton().click();
      await settle();
      expect(checkButton().disabled).toBe(true);
      expect(checkButton().textContent).toContain('Checking…');
      checkButton().click(); // a double click sends nothing more
      const request = http.expectOne(isStatusRequest);
      request.flush({ status: 'delivered' });
      await settle();

      expect(checkButton().disabled).toBe(false);
      expect(checkButton().textContent?.trim()).toBe('Check delivery status');
      expect(badge()).toBe('Delivered to recipient mail server');
      expect(deliveryText()).toContain('Last checked at');
      // A manual check is one read, not a restart of polling.
      await advance(STATUS_POLL_WINDOW_MS);
      http.expectNone(isStatusRequest);
    });

    it('lets a manual check replace the pending automatic one', async () => {
      await submitSuccessfully();
      await advance(STATUS_POLL_INTERVAL_MS - 1000);

      checkButton().click();
      http.expectOne(isStatusRequest).flush({ status: 'queued' });
      await settle();

      // The automatic read that was due in 1 s was replaced; the next is a full interval on.
      await advance(1000);
      http.expectNone(isStatusRequest);
      await advance(STATUS_POLL_INTERVAL_MS - 1000);
      http.expectOne(isStatusRequest).flush({ status: 'queued' });
      await settle();
    });

    it('does not fire the automatic read while a manual check is still waiting', async () => {
      await submitSuccessfully();
      await advance(STATUS_POLL_INTERVAL_MS - 1000);

      checkButton().click();
      const manual = http.expectOne(isStatusRequest);
      // The automatic read was due 1 s after the click; the manual one is still unanswered.
      await advance(3 * STATUS_POLL_INTERVAL_MS);
      expect(http.match(isStatusRequest)).toEqual([]);

      manual.flush({ status: 'queued' });
      await settle();
      await advance(STATUS_POLL_INTERVAL_MS);
      http.expectOne(isStatusRequest).flush({ status: 'queued' });
      await settle();
    });

    it('keeps the last known status and a clear message when a check fails', async () => {
      await submitSuccessfully();
      await answerPoll('queued');

      await advance(STATUS_POLL_INTERVAL_MS);
      http.expectOne(isStatusRequest).flush(null, { status: 0, statusText: 'Unknown Error' });
      await settle();

      const alert = root.querySelector('.delivery [role=alert]');
      expect(alert?.textContent).toContain('Couldn’t check the delivery status');
      expect(alert?.textContent).toContain('Could not reach the server');
      expect(badge()).toBe('Queued by email provider');
      expect(checkButton().disabled).toBe(false);

      // Polling carries on through a temporary failure, and a good answer clears the error.
      await answerPoll('delivered');
      expect(root.querySelector('.delivery [role=alert]')).toBeNull();
      expect(badge()).toBe('Delivered to recipient mail server');
    });

    it('cleans up its timer when the component is destroyed', async () => {
      await submitSuccessfully();

      fixture.destroy();
      await vi.advanceTimersByTimeAsync(STATUS_POLL_WINDOW_MS);

      http.expectNone(isStatusRequest);
    });

    it('stops tracking when the person starts over', async () => {
      await submitSuccessfully();
      await advance(STATUS_POLL_INTERVAL_MS);
      const inFlight = http.expectOne(isStatusRequest);

      root.querySelector<HTMLButtonElement>('[data-action=start-over]')?.click();
      await settle();
      await advance(STATUS_POLL_WINDOW_MS);

      expect(inFlight.cancelled).toBe(true);
      http.expectNone(isStatusRequest);
    });

    // The required wording, spelled out rather than read back from the map the UI uses.
    const REQUIRED_LABELS: Record<DeliveryStatus, string> = {
      pending: 'Preparing your email',
      accepted: 'Accepted for delivery',
      queued: 'Queued by email provider',
      retrying: 'Delivery is being retried',
      delivered: 'Delivered to recipient mail server',
      soft_bounced: 'Temporarily rejected',
      hard_bounced: 'Delivery failed',
      blocked: 'Blocked by email provider',
      failed: 'Could not send email',
    };

    it.each(DELIVERY_STATUSES.map((status) => [status, REQUIRED_LABELS[status]]))(
      'shows %s as "%s", never as the raw backend value',
      async (status, label) => {
        await submitSuccessfully();
        await answerPoll(status);

        expect(badge()).toBe(label);
        expect(deliveryText()).toContain(DELIVERY_STATUS_VIEWS[status].description);
        if (status.includes('_')) {
          expect(root.textContent).not.toContain(status);
        }
      },
    );

    it('says what "delivered" means without promising Inbox placement', async () => {
      await submitSuccessfully();
      await answerPoll('delivered');

      const text = deliveryText();
      expect(text).toContain(
        'The recipient’s mail server accepted the message. Check your Inbox, Spam, or Junk folder.',
      );
      expect(text).not.toMatch(/in your inbox|reached your inbox|guarantee|successfully/i);
    });
  });
});
