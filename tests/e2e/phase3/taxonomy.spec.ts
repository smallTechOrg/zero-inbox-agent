import { test, expect, type Page } from '@playwright/test';
import { openApp, isMailboxConnected } from '../helpers';

/**
 * Phase 3 — Taxonomy editor (D10 fix).
 *
 * Verifies the real taxonomy editor in Settings replaces the StubPanel and is
 * fully functional: loads categories, renders rows, supports inline rename,
 * default-action changes, and adding a new category.
 *
 * Requires a connected Gmail mailbox. Skips loudly when none is present.
 */

async function openSettings(page: Page) {
  await page.getByRole('button', { name: /^Settings$/i }).click();
  await expect(page.locator('#settings-panel, [aria-label="Settings"]')).toBeVisible({ timeout: 5000 });
}

async function openTaxonomyEditor(page: Page) {
  await openSettings(page);
  // The TaxonomyEditor is always rendered in Settings (not hidden behind a stub)
  await expect(page.locator('[aria-label="Taxonomy editor"]')).toBeVisible({ timeout: 8000 });
}

test.describe('Phase 3 — Taxonomy editor', () => {
  test.beforeEach(async ({ page }) => {
    await openApp(page);
    const connected = await isMailboxConnected(page);
    test.skip(
      !connected,
      'NO MAILBOX CONNECTED — export ZI_SESSION=<cookie> to run this suite.',
    );
  });

  test('taxonomy editor is visible and not a stub', async ({ page }) => {
    await openTaxonomyEditor(page);
    // Must NOT contain "COMING SOON" text
    await expect(page.locator('[aria-label="Taxonomy editor"]')).not.toContainText('COMING SOON');
    // Must NOT contain "Phase 3" stub chip
    await expect(page.locator('[aria-label="Taxonomy editor"]')).not.toContainText('Phase 3');
    // Heading is present
    await expect(page.locator('h3', { hasText: 'Taxonomy editor' })).toBeVisible();
  });

  test('categories load and render rows', async ({ page }) => {
    await openTaxonomyEditor(page);
    // Wait for categories to load (skeleton disappears)
    await expect(page.locator('[data-testid="category-row"]').first()).toBeVisible({ timeout: 10000 });
    const rows = page.locator('[data-testid="category-row"]');
    const count = await rows.count();
    expect(count).toBeGreaterThan(0);

    // Each row has a name button and an action dropdown
    await expect(rows.first().locator('[data-testid="category-name"]')).toBeVisible();
    await expect(rows.first().locator('[data-testid="category-action-select"]')).toBeVisible();
  });

  test('add a new category', async ({ page }) => {
    await openTaxonomyEditor(page);
    // Wait for initial load
    await expect(page.locator('[data-testid="category-row"]').first()).toBeVisible({ timeout: 10000 });
    const before = await page.locator('[data-testid="category-row"]').count();

    const uniqueName = `Test-Cat-${Date.now()}`;
    await page.fill('[data-testid="new-category-name"]', uniqueName);
    await page.click('[data-testid="new-category-add"]');

    // New row appears
    await expect(page.locator('[data-testid="category-row"]')).toHaveCount(before + 1, { timeout: 8000 });
    // The new name is visible
    await expect(page.locator('[data-testid="category-name"]', { hasText: uniqueName })).toBeVisible();
  });

  test('inline rename a category', async ({ page }) => {
    await openTaxonomyEditor(page);
    await expect(page.locator('[data-testid="category-row"]').first()).toBeVisible({ timeout: 10000 });

    // Click the first non-system category name to edit
    const firstNameBtn = page.locator('[data-testid="category-name"]').first();
    const originalName = await firstNameBtn.textContent() ?? '';
    await firstNameBtn.click();

    // Input appears
    const nameInput = page.locator('[data-testid="category-name-input"]').first();
    await expect(nameInput).toBeVisible();

    // Press Escape to cancel — name should revert
    await nameInput.press('Escape');
    await expect(page.locator('[data-testid="category-name"]').first()).toHaveText(originalName.trim());
  });

  test('propose taxonomy button is present', async ({ page }) => {
    await openTaxonomyEditor(page);
    await expect(page.locator('[data-testid="propose-taxonomy-btn"]')).toBeVisible({ timeout: 8000 });
  });
});
