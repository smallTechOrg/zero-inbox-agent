import { expect, test } from '@playwright/test';
import { APP_URL } from '../helpers';
import { stubApp, USER_EMAIL } from './fixtures';

/**
 * Screens 22 and 23 — the account menu and Account & security.
 *
 * ⚠️  Nothing destructive is EXECUTED here. The specs open the confirm modals
 * and assert the gate, then back out. Every route is intercepted anyway, so no
 * real session, mailbox or account could be reached even if a click slipped.
 */

async function openAccountSection(page: import('@playwright/test').Page) {
  await page.goto(APP_URL, { waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId('account-menu-trigger')).toBeVisible();
  await page.getByTestId('account-menu-trigger').click();
  await page.getByTestId('account-menu-account').click();
  await expect(page.getByTestId('account-section')).toBeVisible();
}

test.describe('Phase 8 — account & security', () => {
  test('the account menu is a real keyboard-operable menu', async ({ page }) => {
    await stubApp(page, { run: 'completed' });
    await page.goto(APP_URL, { waitUntil: 'domcontentloaded' });

    const trigger = page.getByTestId('account-menu-trigger');
    await expect(trigger).toBeVisible();
    await expect(trigger).toContainText(USER_EMAIL);
    await expect(trigger).toHaveAttribute('aria-haspopup', 'menu');

    await trigger.focus();
    await page.keyboard.press('Enter');
    const menu = page.getByTestId('account-menu');
    await expect(menu).toBeVisible();
    await expect(menu).toContainText('Signed in as');
    await expect(page.getByTestId('account-menu-account')).toHaveText('Account & security');
    await expect(page.getByTestId('account-menu-settings')).toHaveText('Settings');
    await expect(page.getByTestId('account-menu-signout')).toHaveText('Sign out');

    // Arrow keys move the roving focus; Escape closes and returns it to the trigger.
    await page.keyboard.press('ArrowDown');
    await expect(page.getByTestId('account-menu-settings')).toBeFocused();
    await page.keyboard.press('Escape');
    await expect(menu).toHaveCount(0);
    await expect(trigger).toBeFocused();
  });

  test('sign out returns to the homepage, and a reload stays there', async ({ page }) => {
    const log = await stubApp(page, { run: 'completed', logoutSignsOut: true });
    await page.goto(APP_URL, { waitUntil: 'domcontentloaded' });

    await page.getByTestId('account-menu-trigger').click();
    await page.getByTestId('account-menu-signout').click();

    await expect(page.getByTestId('homepage')).toBeVisible();
    expect(log.requests).toContain('POST /auth/logout');

    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.getByTestId('homepage')).toBeVisible();
    await expect(page.getByTestId('account-menu-trigger')).toHaveCount(0);
  });

  test('screen 22 shows identity, the mailbox, sessions and the same-mailbox explanation', async ({
    page,
  }) => {
    await stubApp(page, { run: 'completed', sessions: 2 });
    await openAccountSection(page);

    // You — Google identity, no password to change.
    await expect(page.getByTestId('account-identity')).toBeVisible();
    await expect(page.getByTestId('account-email')).toHaveText(USER_EMAIL);
    await expect(page.getByTestId('account-section')).toContainText(
      'there is no Zero Inbox password to change',
    );

    // The globally-unique-mailbox explanation, in the user's terms.
    await expect(page.getByTestId('account-identity')).toContainText(
      'can never connect the same mailbox',
    );
    await expect(page.getByTestId('account-identity')).toContainText('mailbox_already_connected');

    // Connected mailbox, status as TEXT.
    await expect(page.getByTestId('connection-row')).toHaveCount(1);
    await expect(page.getByTestId('connection-status')).toHaveText('Connected');
    await expect(page.getByTestId('connection-disconnect')).toBeEnabled();

    // Sessions, with the current one named and revocable.
    await expect(page.getByTestId('session-row')).toHaveCount(2);
    await expect(page.getByTestId('session-current')).toHaveText('This device');
    await expect(page.getByTestId('session-revoke').first()).toBeEnabled();
    await expect(page.getByTestId('account-sign-out')).toBeVisible();
    await expect(page.getByTestId('account-sign-out-everywhere')).toBeVisible();
  });

  test('disconnect names its exact consequence and states it is not an undo', async ({ page }) => {
    await stubApp(page, { run: 'completed' });
    await openAccountSection(page);

    await page.getByTestId('connection-disconnect').click();
    const modal = page.getByTestId('disconnect-modal');
    await expect(modal).toBeVisible();
    await expect(modal).toContainText('nothing in Gmail changes');
    await expect(modal).toContainText('nothing is un-archived and nothing is deleted');
    await expect(modal).toContainText('Disconnecting is not an undo');

    // Back out — this spec never disconnects anything.
    await page.getByTestId('disconnect-modal-cancel').click();
    await expect(modal).toHaveCount(0);
  });

  test('account deletion is gated on typing the real email, and names the real counts', async ({
    page,
  }) => {
    await stubApp(page, { run: 'completed' });
    await openAccountSection(page);

    await expect(page.getByTestId('account-danger-zone')).toContainText('Delete account');
    await expect(page.getByTestId('account-danger-zone')).toContainText('894'); // decisions
    await expect(page.getByTestId('account-danger-zone')).toContainText(
      'Your Gmail is untouched',
    );

    await page.getByTestId('account-delete-open').click();
    const modal = page.getByTestId('delete-account-modal');
    await expect(modal).toBeVisible();
    await expect(modal).toContainText('This cannot be undone');

    const confirm = page.getByTestId('delete-account-modal-confirm');
    // Disabled — and it says WHY, never opacity alone (spec/ui.md § Component states).
    await expect(confirm).toBeDisabled();
    await expect(confirm).toHaveAttribute('title', new RegExp(`Type ${USER_EMAIL}`));

    await page.getByTestId('delete-account-confirm-email').fill('someone-else@example.com');
    await expect(confirm).toBeDisabled();
    await expect(modal).toContainText('That does not match your account email');

    await page.getByTestId('delete-account-confirm-email').fill(USER_EMAIL);
    await expect(confirm).toBeEnabled();

    // The gate is proven. The deletion is NOT executed.
    await page.getByTestId('delete-account-modal-cancel').click();
    await expect(modal).toHaveCount(0);
  });

  test('nothing enterprise is promised — not even as “coming soon”', async ({ page }) => {
    await stubApp(page, { run: 'completed' });
    await openAccountSection(page);

    const body = page.locator('body');
    await expect(body).not.toHaveText(/\bSSO\b|\bSAML\b|\bSCIM\b|\bRBAC\b/i);
    await expect(body).not.toHaveText(/role[- ]based access/i);
    await expect(body).not.toHaveText(/audit[- ]log export|export audit/i);
    await expect(body).not.toHaveText(/organisations?|organizations?\s+\(coming/i);
  });
});
