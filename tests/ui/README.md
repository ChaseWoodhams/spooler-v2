# Spooler browser regressions

Install dependencies with `npm ci`, install the test browser with `npx playwright install chromium`, then run `npm run test:ui`.

The suites exercise the public browser interface with deterministic API and WebSocket fixtures, serving the real frontend assets from a local static server. They do not contact configured printers or modify saved Projects, Spoolman inventory, credentials, Queue or backups. Service workers are blocked where needed so network fixtures cannot be bypassed. Existing Python API/integration tests verify server guarantees separately.

Individual suites can be run using `node tests/ui/<suite>.spec.cjs`.

Coverage includes responsive navigation, operator recovery and settings, History, Quick print and Job review, Project stages, and measured dashboard contrast/focus/reduced motion in both themes. These checks complement manual screen-reader and real camera review; they do not establish full WCAG conformance.
