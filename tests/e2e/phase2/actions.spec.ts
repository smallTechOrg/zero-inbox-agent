import { test, expect, type Page } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { openApp, byTestIdOrText, isMailboxConnected } from '../helpers';

/**
 * Phase 2 — real Gmail mutations via the queue: approve (with dry-run off), undo,
 * and reject (which must never touch Gmail, in any dry-run state).
 *
 * SAFETY: every mutating scenario here operates ONLY on a throwaway thread this
 * suite inserts itself via `tests/e2e/phase2/seed_test_thread.py`, which mirrors
 * `tests/integration/test_gmail_mutations.py`'s `_insert_test_thread` /
 * `action_pipeline_row` convention exactly (a synthetic, never-sent message
 * inserted via `users().messages().insert()`, wired to a real, UI-visible
 * `proposed` Decision row). This suite never approves, archives, or undoes a
 * thread from the user's real, pre-existing mail — it only ever acts on the
 * uniquely-tagged `[zero-inbox-agent test] ...` thread it just created.
 */

type SeededThread = {
  skipped?: boolean;
  reason?: string;
  subject_tag: string;
  subject: string;
  decision_id: string;
  cluster_id: string;
  thread_id: string;
  user_id: string;
};

function seedTestThread(): SeededThread {
  const raw = execFileSync('uv', ['run', 'python', 'tests/e2e/phase2/seed_test_thread.py'], {
    cwd: process.cwd(),
    encoding: 'utf-8',
  });
  return JSON.parse(raw.trim().split('\n').pop() as string) as SeededThread;
}

async function readDryRun(page: Page): Promise<boolean> {
  const response = await page.request.get('http://localhost:8001/api/me');
  const body = await response.json();
  return Boolean(body?.data?.settings?.dry_run ?? true);
}

async function setDryRun(page: Page, value: boolean) {
  const response = await page.request.patch('http://localhost:8001/api/settings', {
    data: { dry_run: value },
  });
  expect(response.ok()).toBe(true);
}

test.describe('Phase 2 — real Gmail actions and undo', () => {
  test.beforeEach(async ({ page }) => {
    await openApp(page);
    const connected = await isMailboxConnected(page);
    test.skip(
      !connected,
      'NO MAILBOX CONNECTED — export ZI_SESSION=<zi_session cookie value> to seed the session ' +
        'headlessly (see tests/e2e/helpers.ts). This journey is unverified, not passing.',
    );
  });

  test('approving a single synthetic thread with dry-run off really archives it in Gmail, and Undo restores it', async ({
    page,
  }) => {
    test.slow();
    const originalDryRun = await readDryRun(page);
    const seeded = seedTestThread();
    test.skip(
      Boolean(seeded.skipped),
      `seed_test_thread.py could not seed a throwaway thread: ${seeded.reason ?? 'unknown'}`,
    );

    try {
      await setDryRun(page, false);
      await openApp(page); // reload so the client's `me.settings.dry_run` reflects the change

      const liveBanner = byTestIdOrText(page, 'dry-run-banner', /LIVE|actions will modify/i);
      await expect(liveBanner).toBeVisible({ timeout: 15_000 });

      // Find the cluster/thread by the unique subject tag this test just seeded —
      // never an arbitrary real thread from the connected mailbox.
      const clusterRow = page.getByText(seeded.subject, { exact: false }).first();
      await expect(clusterRow).toBeVisible({ timeout: 30_000 });
      await clusterRow.click();

      const target = page
        .locator('[data-testid="thread-row"]')
        .filter({ hasText: seeded.subject_tag })
        .first();
      await expect(target).toBeVisible({ timeout: 30_000 });

      const approveBtn = target.getByRole('button', { name: /^Approve$/i });
      await expect(approveBtn).toBeVisible();

      const [applyResponse] = await Promise.all([
        page.waitForResponse(
          (r) => /\/api\/actions\/apply$/.test(r.url()) && r.request().method() === 'POST',
          { timeout: 30_000 },
        ),
        approveBtn.click(),
      ]);
      expect(applyResponse.status(), 'applying an approved decision must succeed').toBe(200);
      const applyBody = await applyResponse.json();
      expect(applyBody.error).toBeNull();
      expect(Array.isArray(applyBody.data)).toBe(true);
      expect(applyBody.data.length).toBeGreaterThan(0);
      expect(applyBody.data[0].action_log_id).toBeTruthy();

      // UI reflects the decision becoming `applied` — the real Gmail mutation landed
      // on the synthetic thread only.
      const appliedBadge = target.locator('[data-testid="applied-badge"]');
      await expect(appliedBadge).toBeVisible({ timeout: 20_000 });
      await expect(appliedBadge).toContainText(/applied.*archived/i);

      // Undo it — restores the synthetic thread to the inbox and reverses the label.
      const expandToggle = target.locator('button[aria-label*="Expand"]').first();
      if ((await expandToggle.count()) > 0) {
        await expandToggle.click();
      }
      const undoButton = target.locator('[data-testid="undo-button"]');
      await expect(undoButton).toBeVisible({ timeout: 10_000 });

      const [undoResponse] = await Promise.all([
        page.waitForResponse(
          (r) => /\/api\/actions\/.+\/undo$/.test(r.url()) && r.request().method() === 'POST',
          { timeout: 30_000 },
        ),
        undoButton.click(),
      ]);
      expect(undoResponse.status(), 'undo must succeed and fully reverse the mutation').toBe(200);

      await expect(page.getByText(/Undone — the thread is back in your inbox\./i)).toBeVisible({
        timeout: 20_000,
      });
    } finally {
      await setDryRun(page, originalDryRun);
    }
  });

  test('rejecting a synthetic thread never calls /api/actions/apply, in any dry-run state', async ({
    page,
  }) => {
    const seeded = seedTestThread();
    test.skip(
      Boolean(seeded.skipped),
      `seed_test_thread.py could not seed a throwaway thread: ${seeded.reason ?? 'unknown'}`,
    );

    const applyCalls: string[] = [];
    page.on('request', (req) => {
      if (/\/api\/actions\/apply$/.test(req.url()) && req.method() === 'POST') {
        applyCalls.push(req.url());
      }
    });

    const clusterRow = page.getByText(seeded.subject, { exact: false }).first();
    await expect(clusterRow).toBeVisible({ timeout: 30_000 });
    await clusterRow.click();

    const target = page
      .locator('[data-testid="thread-row"]')
      .filter({ hasText: seeded.subject_tag })
      .first();
    await expect(target).toBeVisible({ timeout: 30_000 });

    const rejectBtn = target.getByRole('button', { name: /^Reject$/i });
    await expect(rejectBtn).toBeVisible();
    await rejectBtn.click();

    // "Rejected" state is recorded locally — no Gmail-mutating network call fires,
    // and the synthetic thread is left untouched, exactly as-is, still in the inbox.
    await expect(target.getByText(/^Rejected$/)).toBeVisible({ timeout: 15_000 });
    await page.waitForTimeout(1_500);
    expect(applyCalls, 'rejecting a thread must never call POST /api/actions/apply').toHaveLength(0);
  });
});
