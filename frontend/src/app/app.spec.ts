import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { ComponentFixture, TestBed } from '@angular/core/testing';

import { App } from './app';

describe('App', () => {
  let fixture: ComponentFixture<App>;
  let root: HTMLElement;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [App],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();
    fixture = TestBed.createComponent(App);
    root = fixture.nativeElement as HTMLElement;
    await fixture.whenStable();
  });

  it('shows the logo and application name in the header', () => {
    const header = root.querySelector('header');

    expect(header?.querySelector('app-logo svg')).not.toBeNull();
    expect(header?.textContent).toContain('Email Confirmation');
  });

  it('renders the form inside the main landmark', () => {
    expect(root.querySelector('main app-submission-form form')).not.toBeNull();
    expect(root.querySelector('h1')?.textContent).toContain('Confirm your email address');
  });

  it('credits the author and links to the repository on GitHub', () => {
    const footer = root.querySelector('footer');
    const link = footer?.querySelector<HTMLAnchorElement>('a[href^="https://github.com/"]');

    // Literal values on purpose: the test must fail if the constants in app.ts drift.
    expect(footer?.textContent).toContain('Made by Daniyar Ismailov');
    expect(link?.getAttribute('href')).toBe('https://github.com/zsz13/email-confirmation-system');
    expect(link?.getAttribute('rel')).toContain('noopener');
    expect(link?.querySelector('svg')).not.toBeNull();
    expect(link?.textContent).toContain('GitHub');
  });
});
