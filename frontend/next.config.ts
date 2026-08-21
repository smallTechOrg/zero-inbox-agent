import type { NextConfig } from 'next'

/**
 * The dashboard runs on :3000 (`pnpm --dir frontend dev`) and the FastAPI
 * backend on :8001 (spec/roadmap.md "How the user tests it"). Every `/api` and
 * `/auth` path is proxied so the browser stays same-origin — the session
 * cookie and the SSE feed both ride the proxy with zero CORS configuration.
 */
const API_ORIGIN = process.env.API_ORIGIN ?? 'http://localhost:8001'

const config: NextConfig = {
  async rewrites() {
    return [
      { source: '/api/:path*', destination: `${API_ORIGIN}/api/:path*` },
      { source: '/auth/:path*', destination: `${API_ORIGIN}/auth/:path*` },
    ]
  },
}

export default config
