import { ChangeDetectionStrategy, Component, signal } from '@angular/core';

import { SubmissionForm } from './features/submission/submission-form';
import { Logo } from './shared/logo';

export const APP_NAME = 'Email Confirmation';
export const AUTHOR_NAME = 'Daniyar Ismailov';
export const REPOSITORY_URL = 'https://github.com/zsz13/email-confirmation-system';

@Component({
  selector: 'app-root',
  imports: [SubmissionForm, Logo],
  templateUrl: './app.html',
  styleUrl: './app.css',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class App {
  protected readonly appName = APP_NAME;
  protected readonly authorName = AUTHOR_NAME;
  protected readonly repositoryUrl = REPOSITORY_URL;
  /** The intro is for filling in the form; once the confirmation is sent it gives way. */
  protected readonly sent = signal(false);
}
