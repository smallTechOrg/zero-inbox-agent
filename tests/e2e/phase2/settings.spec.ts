import { test, expect, type Page } from '@playwright/test';
import { openApp, byTestIdOrText, isMailboxConnected } from '../helpers';

/**
 * Phase 2 — Settings panel: dry-run toggle, VIP editor, priorities profile.
 *
 * Requires a real connected Gmail mailbox (see tests/e2e/helpers.ts for how to seed
 * ZI_SESSION). Skips loudly, never silently, when no mailbox is connected.
 *
 * The dry-run toggle is a *global* setting shared with every other e2e suite, so this
 * file always restores the value it found at the start, in a `finally`, regardless of
 * pass/fail — leaving no side effects for tests/e2e/smoke.spec.ts or triage.spec.ts,
 * which assert the DRY RUN (on) banner.
 */

async function openSettings(page: Page) {
  await page.getByRole('button', { name: /^Settings$/i }).click();
  await expect(page.locator('#settings-panel')).toBeVisible();
}

async function readDryRun(page: Page): Promise<boolean> {
  const response = await page.request.get('http://localhost:8001/api/me');
  const body = await response.json();
  return Boolean(body?.data?.settings?.dry_run ?? true);
}

test.describe('Phase 2 — Settings', () => {
  test.beforeEach(async ({ page }) => {
    await openApp(page);
    const connected = await isMailboxConnected(page);
    test.skip(
      !connected,
      'NO MAILBOX CONNECTED — export ZI_SESSION=<zi_session cookie value> to seed the session ' +
        'headlessly (see tests/e2e/helpers.ts). This journey is unverified, not passing.',
    );
  });

  test('turning dry-run off flips the banner from DRY RUN to LIVE, and back again', async ({
    page,
  }) => {
    const originalDryRun = await readDryRun(page);

    await openSettings(page);

    const toggle = page.locator('[data-testid="dry-run-toggle"]');
    await expect(toggle).toBeVisible();

    try {
      // --- Turn dry-run OFF ---
      if ((await toggle.getAttribute('aria-pressed')) !== 'false') {
        await toggle.click();
      }
      const saveBtn = page.locator('[data-testid="settings-save"]');
      await expect(saveBtn).toBeEnabled();
      const [patchOff] = await Promise.all([
        page.waitForResponse(
          (r) => /\/api\/settings$/.test(r.url()) && r.request().method() === 'PATCH',
          { timeout: 15_000 },
        ),
        saveBtn.click(),
      ]);
      expect(patchOff.status()).toBe(200);
      const offBody = await patchOff.json();
      expect(offBody.data.dry_run).toBe(false);

      // Banner reflects the new state: no more "DRY RUN", now LIVE.
      const banner = byTestIdOrText(page, 'dry-run-banner', /LIVE|actions will modify/i);
      await expect(banner).toBeVisible({ timeout: 10_000 });
      await expect(banner).toContainText(/live|will modify your gmail/i);
      await expect(banner).not.toContainText(/nothing in your gmail has been changed/i);

      // --- Turn dry-run back ON ---
      await openSettings(page);
      const toggle2 = page.locator('[data-testid="dry-run-toggle"]');
      if ((await toggle2.getAttribute('aria-pressed')) !== 'true') {
        await toggle2.click();
      }
      const saveBtn2 = page.locator('[data-testid="settings-save"]');
      const [patchOn] = await Promise.all([
        page.waitForResponse(
          (r) => /\/api\/settings$/.test(r.url()) && r.request().method() === 'PATCH',
          { timeout: 15_000 },
        ),
        saveBtn2.click(),
      ]);
      expect(patchOn.status()).toBe(200);

      const bannerBack = byTestIdOrText(page, 'dry-run-banner', /DRY RUN/i);
      await expect(bannerBack).toBeVisible({ timeout: 10_000 });
      await expect(bannerBack).toContainText(/nothing in your gmail has been changed/i);
    } finally {
      // Best-effort restore to whatever the mailbox had before this test, in case an
      // assertion threw mid-sequence and left dry_run in the wrong state.
      const current = await readDryRun(page);
      if (current !== originalDryRun) {
        await page.request.patch('http://localhost:8001/api/settings', {
          data: { dry_run: originalDryRun },
        });
      }
    }
  });

  test('the auto-act threshold slider persists a new value', async ({ page }) => {
    await openSettings(page);

    const slider = page.locator('[data-testid="slider-auto-act-threshold"]');
    await expect(slider).toBeVisible();

    await slider.fill('0.9');
    const saveBtn = page.locator('[data-testid="settings-save"]');
    await expect(saveBtn).toBeEnabled();

    const [response] = await Promise.all([
      page.waitForResponse(
        (r) => /\/api\/settings$/.test(r.url()) && r.request().method() === 'PATCH',
        { timeout: 15_000 },
      ),
      saveBtn.click(),
    ]);
    expect(response.status()).toBe(200);
    const body = await response.json();
    expect(body.data.auto_act_threshold).toBeCloseTo(0.9, 1);
  });

  test('VIP editor: adding an entry shows it in the list, removing it clears it', async ({
    page,
  }) => {
    await openSettings(page);

    const uniqueEmail = `e2e-vip-${Date.now()}@example.com`;

    await page.locator('[data-testid="vip-kind"]').selectOption('email');
    await page.locator('[data-testid="vip-value"]').fill(uniqueEmail);

    const [addResponse] = await Promise.all([
      page.waitForResponse(
        (r) => /\/api\/vip$/.test(r.url()) && r.request().method() === 'POST',
        { timeout: 15_000 },
      ),
      page.locator('[data-testid="vip-add"]').click(),
    ]);
    expect(addResponse.status()).toBe(200);

    const row = page.locator('[data-testid="vip-row"]', { hasText: uniqueEmail });
    await expect(row).toBeVisible({ timeout: 10_000 });

    const removeButton = row.locator('[data-testid="vip-remove"]');
    const [removeResponse] = await Promise.all([
      page.waitForResponse(
        (r) => /\/api\/vip\/.+/.test(r.url()) && r.request().method() === 'DELETE',
        { timeout: 15_000 },
      ),
      removeButton.click(),
    ]);
    expect(removeResponse.status()).toBe(200);

    await expect(page.locator('[data-testid="vip-row"]', { hasText: uniqueEmail })).toHaveCount(0);
  });

  test('priorities profile: saved text survives a full page reload', async ({ page }) => {
    await openSettings(page);

    const uniqueText = `E2E priority note ${Date.now()} — investors and fundraise mail matters most.`;
    const textarea = page.locator('[data-testid="priorities-profile-text"]');
    await expect(textarea).toBeVisible();
    await textarea.fill(uniqueText);

    const saveButton = page.locator('[data-testid="priorities-profile-save"]');
    await expect(saveButton).toBeEnabled();
    const [response] = await Promise.all([
      page.waitForResponse(
        (r) => /\/api\/profile$/.test(r.url()) && r.request().method() === 'PUT',
        { timeout: 15_000 },
      ),
      saveButton.click(),
    ]);
    expect(response.status()).toBe(200);
    const body = await response.json();
    expect(body.data.text).toBe(uniqueText);

    // Reload the whole app — persistence must survive a real navigation, not just
    // in-memory React state.
    await openApp(page);
    await openSettings(page);

    const reloadedTextarea = page.locator('[data-testid="priorities-profile-text"]');
    await expect(reloadedTextarea).toHaveValue(uniqueText, { timeout: 10_000 });
  });
});
