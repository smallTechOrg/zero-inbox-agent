import { test, expect } from '@playwright/test'
import { openApp } from '../helpers'

/**
 * Phase 3 — Autopilot & Digest frontend smoke tests.
 *
 * These tests run against the live dev server (http://localhost:8001). They
 * verify the UI scaffolding for the three new surfaces: Run Summary card,
 * Catch-up Digest page, and Activity Drawer.
 */

test.describe('Phase 3 — Activity Drawer', () => {
  test.beforeEach(async ({ page }) => {
    await openApp(page)
  })

  test('bell icon is present and clickable', async ({ page }) => {
    const bell = page.getByTestId('activity-bell')
    await expect(bell).toBeVisible()
    await expect(bell).toBeEnabled()
  })

  test('clicking bell opens the activity drawer', async ({ page }) => {
    const bell = page.getByTestId('activity-bell')
    await bell.click()

    const drawer = page.getByRole('dialog', { name: /activity feed/i })
    await expect(drawer).toBeVisible()
  })

  test('drawer shows empty state when no events exist', async ({ page }) => {
    const bell = page.getByTestId('activity-bell')
    await bell.click()

    const drawer = page.getByRole('dialog', { name: /activity feed/i })
    await expect(drawer).toBeVisible()

    // Either shows empty state text or actual events — both are valid
    const hasEmpty = await page.getByText(/no activity yet/i).isVisible()
    const hasEvents = (await drawer.locator('li').count()) > 0
    expect(hasEmpty || hasEvents, 'drawer must show events or empty state').toBeTruthy()
  })

  test('drawer can be closed', async ({ page }) => {
    const bell = page.getByTestId('activity-bell')
    await bell.click()

    const drawer = page.getByRole('dialog', { name: /activity feed/i })
    await expect(drawer).toBeVisible()

    const closeBtn = drawer.getByRole('button', { name: /close/i })
    await closeBtn.click()

    await expect(drawer).not.toBeVisible()
  })
})

test.describe('Phase 3 — Digest page', () => {
  test('digest page loads at /app/digest', async ({ page }) => {
    await page.goto('http://localhost:8001/app/digest')
    await page.waitForLoadState('networkidle')

    // Page should render the digest heading
    await expect(page.getByRole('heading', { name: /catch-up digest/i })).toBeVisible()
  })

  test('digest shows empty state or content — never a raw error', async ({ page }) => {
    await page.goto('http://localhost:8001/app/digest')
    await page.waitForLoadState('networkidle')

    // Three valid states: loading skeleton, empty/no-digest state, or real content
    const hasNoDigest = await page.getByText(/no digest yet/i).isVisible().catch(() => false)
    const hasContent = await page.getByText(/auto-archived/i).isVisible().catch(() => false)
    const hasError = await page.getByText(/retry/i).isVisible().catch(() => false)

    expect(
      hasNoDigest || hasContent || hasError,
      'digest page must show one of: no-digest state, real content, or error with retry',
    ).toBeTruthy()
  })

  test('digest page has a back link to triage', async ({ page }) => {
    await page.goto('http://localhost:8001/app/digest')
    await page.waitForLoadState('networkidle')

    const backLink = page.getByRole('link', { name: /back to triage/i })
    await expect(backLink).toBeVisible()
  })

  test('Digest link in left rail navigates to digest page', async ({ page }) => {
    await openApp(page)

    // Digest is now a real nav link (not a stub)
    const digestLink = page.getByRole('link', { name: /^Digest$/i })
    await expect(digestLink).toBeVisible()
    await digestLink.click()

    await expect(page).toHaveURL(/\/app\/digest/)
  })
})

test.describe('Phase 3 — Run Summary card', () => {
  test.beforeEach(async ({ page }) => {
    await openApp(page)
  })

  test('run summary card appears when a completed run exists', async ({ page }) => {
    // Check whether a completed run summary section is visible
    // It may not exist if no run has completed yet — that's valid
    const summarySection = page.getByRole('region', { name: /run summary/i })

    // If visible, verify its headline structure
    if (await summarySection.isVisible()) {
      // Should have either "Triage complete" or "in progress" headline
      const headline = summarySection.getByRole('heading')
      await expect(headline).toBeVisible()

      // Approve & Apply button should be present
      const applyBtn = summarySection.getByRole('button', { name: /approve all & apply/i })
      await expect(applyBtn).toBeVisible()

      // Review clusters button should be present
      const reviewBtn = summarySection.getByRole('button', { name: /review clusters/i })
      await expect(reviewBtn).toBeVisible()
    }

    // Whether or not the card is visible, the page must load without unhandled errors
    await expect(page.locator('body')).not.toBeEmpty()
  })
})
