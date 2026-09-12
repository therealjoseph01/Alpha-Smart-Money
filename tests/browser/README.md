# Dashboard browser checks

The dashboard has no frontend build step or runtime dependencies. The optional browser check uses Playwright and an installed Chrome (or Playwright Chromium). Every HTTP request is fulfilled from fixtures inside the test: it never contacts the application, providers, wallets, or a trading service.

From the repository root:

```sh
npm install --prefix /tmp/asm-dashboard-check playwright
NODE_PATH=/tmp/asm-dashboard-check/node_modules \
CHROME_PATH='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' \
node tests/browser/dashboard.cjs
```

Omit `CHROME_PATH` when using Playwright's installed Chromium. Set `SCREENSHOT_DIR` to an output directory to save desktop, mobile, and login previews. Screenshots contain simulated data, not account balances.

Coverage: all seven views at 360, 390, 768, 1024, and 1440 CSS pixels; page overflow; navigation state; login errors and logout; chart time-range requests, position shortcut, and empty states; mocked pause/resume; dialog cancellation and focus containment; request failure and recovery; wallet draft preservation across polling; settings editing. These are Chromium viewport checks, not physical-device or Safari tests. Existing Python session tests cover server authentication separately.
