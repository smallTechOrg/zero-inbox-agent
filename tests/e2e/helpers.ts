import { expect, type Page, type Locator } from '@playwright/test';

export const APP_URL = 'http://localhost:8001/app/';

/** Open the dashboard and wait for the client bundle to hydrate. */
export async function openApp(page: Page): Promise<void> {
  await page.goto(APP_URL, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('body')).toBeVisible();
  // Give the static export's JS a moment to render the shell.
  await page.waitForLoadState('networkidle').catch(() => undefined);
}

/**
 * Prefer a stable `data-testid`; fall back to visible text so this suite is not
 * coupled to one particular markup choice.
 */
export function byTestIdOrText(page: Page, testId: string, text: RegExp | string): Locator {
  const byId = page.locator(`[data-testid="${testId}"]`);
  return byId.or(page.getByText(text)).first();
}

/** Is a Gmail mailbox connected for this browser session? */
export async function isMailboxConnected(page: Page): Promise<boolean> {
  const response = await page.request.get('http://localhost:8001/api/me');
  if (!response.ok()) return false;
  const body = await response.json().catch(() => null);
  const connections = body?.data?.connections ?? [];
  return Array.isArray(connections) && connections.length > 0;
}

/** Proof the CSS bundle really loaded — a built page, not an unstyled DOM dump. */
export async function assertPageIsStyled(page: Page): Promise<void> {
  const stylesheetCount = await page.evaluate(() => document.styleSheets.length);
  expect(stylesheetCount, 'no stylesheet loaded — the page is unstyled').toBeGreaterThan(0);

  const bodyBackground = await page.evaluate(
    () => getComputedStyle(document.body).backgroundColor,
  );
  expect(bodyBackground, 'body has no computed background colour').toBeTruthy();

  const hasUtilityRules = await page.evaluate(() => {
    for (const sheet of Array.from(document.styleSheets)) {
      let rules: CSSRuleList;
      try {
        rules = (sheet as CSSStyleSheet).cssRules;
      } catch {
        continue;
      }
      if (rules && rules.length > 20) return true;
    }
    return false;
  });
  expect(hasUtilityRules, 'stylesheet present but essentially empty').toBe(true);
}
