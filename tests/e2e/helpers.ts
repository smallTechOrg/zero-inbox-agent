import { expect, type Page, type Locator } from '@playwright/test';

/** The Next.js dashboard (spec/roadmap.md "How the user tests it"). */
export const APP_URL = 'http://localhost:3000/';

/** The exact stub badge wording — binding per spec/ui.md § Stub convention. */
export const STUB_BADGE_TEXT = 'Coming in Phase 2 — not yet functional';

/**
 * Pre-seed the signed session cookie from the ZI_SESSION env var so the
 * signed-in journey can run headlessly against an already-connected account:
 *
 *   ZI_SESSION=<cookie value> pnpm --dir frontend exec playwright test tests/e2e/phase1
 *
 * Sign in once at http://localhost:3000, copy the session cookie value from
 * devtools (Application → Cookies). When unset, signed-in specs skip with a
 * loud reason — never a silent pass. The backend under test must run with the
 * test-isolation guard so no live Gmail mutation can occur.
 */
export function sessionCookie():
  | { name: string; value: string; domain: string; path: string; httpOnly: boolean; secure: boolean; sameSite: 'Lax' }[]
  | undefined {
  const value = process.env.ZI_SESSION;
  if (!value) return undefined;
  const name = process.env.ZI_SESSION_COOKIE_NAME || 'zi_session';
  return [
    { name, value, domain: 'localhost', path: '/', httpOnly: true, secure: false, sameSite: 'Lax' },
  ];
}

export const hasSession = (): boolean => Boolean(process.env.ZI_SESSION);

export async function openDashboard(page: Page): Promise<void> {
  const cookies = sessionCookie();
  if (cookies) await page.context().addCookies(cookies);
  await page.goto(APP_URL, { waitUntil: 'domcontentloaded' });
  await page.waitForLoadState('networkidle').catch(() => undefined);
}

/** The page must be a styled app, not an unstyled DOM dump or an error page. */
export async function assertPageIsStyled(page: Page): Promise<void> {
  await expect(page.locator('body')).toBeVisible();
  const font = await page.evaluate(() => getComputedStyle(document.body).fontFamily);
  expect(font, 'body has no computed font-family — CSS did not load').toBeTruthy();
  await expect(page.locator('body')).not.toContainText('Application error');
  await expect(page.locator('body')).not.toContainText('Internal Server Error');
  await expect(page.locator('body')).not.toContainText('Traceback');
}

/**
 * Locate a dashboard section. CROSS-SLICE CONTRACT with frontend-dashboard:
 * prefer stable data-testids (`ledger-section`, `costs-section`,
 * `profiles-section`, `taxonomy-panel`, `command-strip`, `run-timeline`,
 * `activity-feed`); fall back to a heading-text match so the suite is not
 * coupled to one markup choice.
 */
export function section(page: Page, testId: string, heading: RegExp): Locator {
  const byId = page.getByTestId(testId);
  const byHeading = page
    .locator('section, [role="region"], div')
    .filter({ has: page.getByRole('heading', { name: heading }) })
    .first();
  return byId.or(byHeading).first();
}

export const stubBadges = (page: Page): Locator => page.getByText(STUB_BADGE_TEXT, { exact: false });
