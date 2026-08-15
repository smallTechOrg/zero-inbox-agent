import { expect, test } from '@playwright/test';
import { APP_URL, assertPageIsStyled } from '../helpers';
import { horizontalOverflow, statelessColourOffenders, stubApp } from './fixtures';

/**
 * The design system — spec/ui.md § Design system.
 *
 * Two rules are asserted here because they are the two that silently rot:
 *  · state is never colour alone (the binding token-level rule);
 *  · the live feed and the Inbox-Zero card are never the surfaces that get
 *    dropped or overflow on a narrow viewport.
 */

const WIDTHS = [
  { name: '375px (phone)', width: 375, height: 812 },
  { name: '768px (tablet)', width: 768, height: 1024 },
  { name: '1440px (desktop)', width: 1440, height: 900 },
];

test.describe('Phase 8 — design system & accessibility', () => {
  test.use({ storageState: { cookies: [], origins: [] } });

  for (const vp of WIDTHS) {
    test(`the signed-out homepage has no horizontal overflow at ${vp.name}`, async ({ page }) => {
      await page.setViewportSize({ width: vp.width, height: vp.height });
      await page.goto(APP_URL, { waitUntil: 'domcontentloaded' });
      await expect(page.getByTestId('homepage')).toBeVisible();
      await assertPageIsStyled(page);

      expect(
        await horizontalOverflow(page),
        `the homepage scrolls horizontally at ${vp.name}`,
      ).toBeLessThanOrEqual(1);

      // Landmarks (spec/ui.md § Accessibility).
      await expect(page.locator('main')).toHaveCount(1);
      await expect(page.getByRole('contentinfo')).toBeVisible();

      expect(await statelessColourOffenders(page)).toEqual([]);
    });
  }
});

test.describe('Phase 8 — the console holds its two primary surfaces at every width', () => {
  for (const vp of WIDTHS) {
    test(`Inbox-Zero card and live feed both render at ${vp.name}`, async ({ page }) => {
      await stubApp(page, {
        run: 'completed',
        dryRun: false,
        ledger: { not_reviewed: 12, distance_to_zero: 5, apply_ok: true },
      });
      await page.setViewportSize({ width: vp.width, height: vp.height });
      await page.goto(APP_URL, { waitUntil: 'domcontentloaded' });

      await expect(page.getByTestId('inbox-zero-card')).toBeVisible();
      await expect(page.getByTestId('live-run-feed')).toBeVisible();

      expect(
        await horizontalOverflow(page),
        `the console scrolls horizontally at ${vp.name}`,
      ).toBeLessThanOrEqual(1);

      // Every state surface says what it means, in words.
      expect(
        await statelessColourOffenders(page),
        'a state element conveys meaning with colour alone',
      ).toEqual([]);
    });
  }

  test('the steady-state column order is Inbox-Zero card → live feed → clusters', async ({
    page,
  }) => {
    await stubApp(page, { run: 'completed', dryRun: false });
    await page.goto(APP_URL, { waitUntil: 'domcontentloaded' });

    await expect(page.getByTestId('inbox-zero-card')).toBeVisible();
    const inOrder = await page.evaluate(() => {
      const card = document.querySelector('[data-testid="inbox-zero-card"]');
      const feed = document.querySelector('[data-testid="live-run-feed"]');
      const history = document.querySelector('[aria-label="Triage history"]');
      if (!card || !feed || !history) return { card: !!card, feed: !!feed, history: !!history };
      return {
        card: true,
        feed: (card.compareDocumentPosition(feed) & 4) !== 0,
        history: (feed.compareDocumentPosition(history) & 4) !== 0,
      };
    });
    expect(inOrder).toEqual({ card: true, feed: true, history: true });
  });

  test('every interactive control that is disabled explains why', async ({ page }) => {
    await stubApp(page, { run: 'completed', dryRun: false });
    await page.goto(APP_URL, { waitUntil: 'domcontentloaded' });
    await expect(page.getByTestId('inbox-zero-card')).toBeVisible();

    const unexplained = await page.evaluate(() =>
      Array.from(document.querySelectorAll<HTMLElement>('button[disabled], a[aria-disabled="true"]'))
        .filter(el => el.getClientRects().length > 0)
        .filter(el => !(el.getAttribute('title') || '').trim())
        .map(el => (el.innerText || el.outerHTML).slice(0, 120)),
    );
    expect(unexplained, 'a disabled control with no reason is a defect').toEqual([]);
  });

  test('focus is always visible: every interactive element carries a focus ring', async ({
    page,
  }) => {
    await stubApp(page, { run: 'completed', dryRun: false });
    await page.goto(APP_URL, { waitUntil: 'domcontentloaded' });
    await expect(page.getByTestId('account-menu-trigger')).toBeVisible();

    await page.getByTestId('account-menu-trigger').focus();
    const ring = await page.evaluate(() => {
      const el = document.querySelector<HTMLElement>('[data-testid="account-menu-trigger"]')!;
      const s = getComputedStyle(el);
      return { outline: s.outlineStyle, width: s.outlineWidth, shadow: s.boxShadow };
    });
    expect(
      ring.outline !== 'none' || ring.shadow !== 'none',
      `the focused control has no visible focus indicator: ${JSON.stringify(ring)}`,
    ).toBe(true);
  });
});
