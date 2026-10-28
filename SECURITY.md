# Security Policy

## Reporting a vulnerability

Please report security issues privately via [GitHub Security Advisories](https://github.com/nicococo/tootflow/security/advisories/new)
or by email to the address in the operator contact (see the hosted instance's Impressum).
Do not open a public issue for vulnerabilities.

We aim to acknowledge reports within a few days and will credit reporters in the fix
release unless they prefer otherwise.

## Security design notes

- **Token storage**: Mastodon, X, and AI provider credentials are encrypted at rest
  (Fernet, key derived from `SECRET_KEY`). Decrypted values are only ever held in
  transient, non-persisted objects — regression tests in the suite enforce this.
- **Auth**: Mastodon OAuth only; no passwords or email. Sessions are signed JWTs in
  `httpOnly` cookies (`Secure` in production).
- **CSRF**: all state-changing routes require a per-session CSRF token.
- **SSRF**: user-supplied instance URLs are validated against private/loopback ranges.
- **Rate limiting**: login, AI generation, and export endpoints are rate-limited.
- **Data**: no email, no analytics cookies, no third-party trackers. GDPR Article 17
  (delete) and Article 20 (export) are implemented as first-class features.

## Scope

TootFlow delegates trust to the platforms it talks to (Mastodon instances, X, your AI
provider). Compromises of those third parties are out of scope — as are issues in
self-hosted deployments with default `SECRET_KEY` values (the app logs a loud warning).
