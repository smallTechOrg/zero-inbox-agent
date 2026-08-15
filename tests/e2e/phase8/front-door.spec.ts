import { expect, test } from '@playwright/test';
import { APP_URL, assertPageIsStyled } from '../helpers';
import { eventSourceUrls, recordEventSources } from './fixtures';

/**
 * Screen 19 — the signed-out front door, asserted LIVE.
 *
 * This is the one Phase 8 spec that talks to the real server: an anonymous
 * visitor and the real `GET /api/me` (401). It is read-only — it starts no run,
 * writes no row and touches no mailbox.
 */

// Anonymous no matter what ZI_SESSION says: this spec is about the signed-out branch.
test.use({ storageState: { cookies: [], origins: [] } });

test.describe('Phase 8 — signed-out front door', () => {
  test('a visitor with no session gets the homepage, not the console', async ({ page }) => {
    const apiCalls: string[] = [];
    page.on('request', r => {
      const u = new URL(r.url());
      if (u.pathname.startsWith('/api/')) apiCalls.push(`${r.method()} ${u.pathname}`);
    });

    await page.goto(APP_URL, { waitUntil: 'domcontentloaded' });
    await expect(page.getByTestId('homepage')).toBeVisible();
    await assertPageIsStyled(page);

    // What the product does, and the promise (spec/ui.md screen 19 hero).
    await expect(page.getByRole('heading', { level: 1 })).toContainText(
      'An inbox that holds only what needs a human',
    );
    await expect(page.locator('main')).toContainText('archives it');
    await expect(page.locator('main')).toContainText('never deletes it');

    // The safety model — the five promises, no more and no fewer.
    await expect(page.getByTestId('safety-promises')).toBeVisible();
    await expect(page.getByTestId('safety-promise')).toHaveCount(5);
    await expect(page.getByTestId('safety-promises')).toContainText(
      'There is no delete, no trash, no spam-report anywhere in the system.',
    );
    await expect(page.getByTestId('safety-promises')).toContainText(
      'People, Urgent and Legal mail can never be set to auto-archive.',
    );

    // EXACTLY ONE call to action. Everything else that navigates is a link.
    await expect(page.getByTestId('primary-cta')).toHaveText('Sign in with Google');
    const buttonLabels = await page.locator('button').allInnerTexts();
    expect(
      buttonLabels.filter(t => t.trim().length > 0).length,
      `more than one visible button on the homepage: ${JSON.stringify(buttonLabels)}`,
    ).toBe(1);

    // No console surface, not even skeletally.
    await expect(page.getByTestId('run-status-pill')).toHaveCount(0);
    await expect(page.getByRole('navigation', { name: 'Main' })).toHaveCount(0);
    await expect(page.getByTestId('cluster-row')).toHaveCount(0);
    await expect(page.getByTestId('inbox-zero-card')).toHaveCount(0);
    await expect(page.getByTestId('dry-run-banner')).toHaveCount(0);
    await expect(page.getByTestId('live-run-feed')).toHaveCount(0);

    // The false Phase-1 promise is gone from the product entirely.
    await expect(page.locator('body')).not.toContainText('Nothing is changed until you say so');

    // Nothing enterprise is promised anywhere — not even as "coming soon".
    await expect(page.locator('body')).not.toHaveText(/\bSSO\b|\bSAML\b|\bSCIM\b|\bRBAC\b/i);
    await expect(page.locator('body')).not.toHaveText(/audit[- ]log export/i);

    // The only API call the front door makes is the session check.
    const distinct = [...new Set(apiCalls)];
    expect(distinct, `signed-out page called extra APIs: ${JSON.stringify(distinct)}`).toEqual([
      'GET /api/me',
    ]);
  });

  test('the honesty numbers are static copy, labelled as the author’s own inbox', async ({
    page,
  }) => {
    const jsonResponses: string[] = [];
    page.on('response', async r => {
      const u = new URL(r.url());
      if (!u.pathname.startsWith('/api/')) return;
      jsonResponses.push(await r.text().catch(() => ''));
    });

    await page.goto(APP_URL, { waitUntil: 'domcontentloaded' });
    const band = page.locator('#honesty');
    await expect(band).toContainText('What it does when it can’t be sure');
    await expect(band).toContainText('1,219');
    await expect(band).toContainText('2,177');
    await expect(band).toContainText('589');

    // Labelled as measured, not presented as the visitor's account.
    await expect(page.getByTestId('honesty-provenance')).toContainText(
      'Measured on the author’s own inbox',
    );
    await expect(page.getByTestId('honesty-provenance')).toContainText('not your account’s');

    // …and NOT fetched: no API response carried any of those figures.
    await page.waitForTimeout(500);
    for (const body of jsonResponses) {
      expect(body, 'an API response carried the honesty-band numbers — they must be static copy')
        .not.toMatch(/1219|2177|589/);
    }
  });

  test('a signed-out visitor never opens the /api/events stream', async ({ page }) => {
    await recordEventSources(page);

    const eventRequests: string[] = [];
    page.on('request', r => {
      if (new URL(r.url()).pathname === '/api/events') eventRequests.push(r.url());
    });

    await page.goto(APP_URL, { waitUntil: 'domcontentloaded' });
    await expect(page.getByTestId('homepage')).toBeVisible();
    // Give any stray effect a generous window to misbehave.
    await page.waitForTimeout(2000);

    expect(await eventSourceUrls(page), 'the marketing page constructed an EventSource').toEqual([]);
    expect(eventRequests, 'the signed-out page requested /api/events — it would 401 and loop')
      .toEqual([]);
    await expect(page.getByTestId('activity-bell')).toHaveCount(0);
  });

  test('the sign-in card states the minimal-scope promise and creates the account', async ({
    page,
  }) => {
    await page.goto(APP_URL, { waitUntil: 'domcontentloaded' });
    await page.getByTestId('primary-cta').click();

    await expect(page.getByTestId('signin-creates-account')).toContainText(
      'Signing in creates your account',
    );
    await expect(page.getByTestId('minimal-scope-promise')).toContainText(
      'asks Google for your name and email address only',
    );
    await expect(page.getByTestId('minimal-scope-promise')).toContainText(
      'separate, later step you approve individually',
    );
    await expect(page.getByTestId('continue-with-google')).toHaveAttribute(
      'href',
      '/auth/google/start?intent=signin',
    );
  });

  test('an OAuth failure renders inline, and says nothing was created', async ({ page }) => {
    await page.goto(`${APP_URL}?auth_error=auth_declined`, { waitUntil: 'domcontentloaded' });
    await expect(page.getByTestId('signin-error')).toContainText('You cancelled sign-in.');
    await expect(page.getByTestId('signin-error')).toContainText('Nothing was created');
  });
});
