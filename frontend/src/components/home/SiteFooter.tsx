'use client'

/** Marketing footer — spec/ui.md screen 19. `contentinfo` landmark.
 *  No newsletter signup, no social icons, no cookie banner: the only cookie is
 *  the session cookie, and the footer text says so. */

import { CONTENT } from '@/lib/tokens'

export function SiteFooter() {
  return (
    <footer className="border-t border-zi-border bg-zi-bg px-6 py-10">
      <div className={`${CONTENT} space-y-4`}>
        <nav aria-label="Footer" className="flex flex-wrap gap-6">
          <a href="#safety-model" className="zi-link zi-body">
            The safety model
          </a>
          <a href="/health" className="zi-link zi-body">
            Service health
          </a>
        </nav>

        <p className="zi-body max-w-3xl text-zi-fg-muted">
          <strong className="font-semibold">Privacy, in plain words.</strong> Zero Inbox stores mail
          headers, subjects and a redacted 200-character snippet — never message bodies, never
          attachments. It never deletes mail and never reports it as spam. The only cookie it sets is
          the session cookie that keeps you signed in; there is no analytics, advertising or
          third-party tracking cookie, which is why there is no cookie banner. You can disconnect
          your mailbox or delete your account, with the exact consequences named, from Account &amp;
          security.
        </p>

        <p className="zi-caption text-zi-fg-faint">Zero Inbox — runs on your own machine.</p>
      </div>
    </footer>
  )
}
