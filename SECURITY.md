# UTmail persistent OWA-session security contract

`utmail` is an unofficial, user-session-authenticated, read-only client for a user-owned University of Toronto Outlook Web mailbox.

## Authentication boundary

- The helper does not register an Entra application and does not use Microsoft Graph.
- The recommended `login --persistent` flow creates a dedicated Chromium profile under the private UTmail state directory. The mailbox owner completes Microsoft/U of T sign-in in that profile when required.
- The profile stores Microsoft/Outlook cookies, MSAL cache entries, and other browser state. These credentials have the same broad practical sensitivity as a signed-in Outlook browser and may permit actions far beyond this CLI.
- The helper additionally stores the currently selected OWA access token and rotating first-party SPA refresh token in `session.json` so ordinary renewals do not need to launch Chromium.
- Microsoft gives SPA refresh tokens an approximately 24-hour hard refresh window. Rotating the token does not make that window permanent. After the hard window ends, the helper starts its dedicated profile headlessly so Outlook can use its signed-in Microsoft cookies to obtain a new authorization grant.
- Continued unattended renewal is best-effort, not literally indefinite. Microsoft or U of T can require human reauthentication after password changes, MFA/Conditional Access events, explicit revocation, inactivity, or policy limits.
- `login --from-vimbrowser` and `login --token-stdin` remain short-lived compatibility modes. They do not enable automatic renewal or copy vimbrowser cookies.
- Credentials are never accepted in argv, printed, logged, included in errors, or emitted as JSON.
- Browser/profile state and `session.json` are protected by a mode `0700` parent directory; `session.json` is mode `0600` and replaced atomically under a process lock. The state is not separately encrypted at rest, so compromise of the owner's Unix account can expose it.
- `utmail logout` deletes the helper-owned access/refresh-token state and the dedicated Chromium profile. It does not revoke Microsoft server-side sessions or sign out unrelated browsers.

## Renewal network boundary

- Direct renewal uses a form-encoded HTTPS POST only to the exact tenant path under `https://login.microsoftonline.com/`.
- The request is fixed to Outlook Web's first-party client ID, `grant_type=refresh_token`, and `https://outlook.office.com/.default`; there is no generic OAuth or endpoint escape hatch.
- Redirects are disabled, response size is bounded, credentials remain in the request body, and OAuth response bodies are never echoed in errors.
- When the approximately 24-hour SPA refresh window ends, the helper-owned Chromium profile may load Microsoft, U of T SSO, and Outlook pages. Outlook itself performs background authentication and application requests. The CLI does not attempt intentional mail mutation, but the stored browser session is not server-side read-only.

## Mailbox network boundary

- All direct mailbox API requests are HTTPS GET requests.
- The only allowed mailbox API origin and prefix are `https://outlook.cloud.microsoft/api/v2.0/`.
- Redirects are disabled. Pagination links must remain under the same exact origin and prefix.
- POST, PATCH, PUT, DELETE, arbitrary mailbox endpoint escape hatches, and mutation commands are absent.
- Request count, pagination, retries, response bytes, attachment bytes, and timeouts are bounded.

## Local boundary

- Session/config/profile directories are mode `0700`; structured credential files are mode `0600` and replaced atomically.
- The browser profile may contain many Chromium-created files with varying individual modes; the mode `0700` profile root prevents access by other local users.
- Downloads are regular files with mode `0600`, reject symlink directories, refuse overwrite without `--force`, and have a fixed size ceiling.
- Inbox/search summaries omit bodies. Only explicit `show` and `thread` commands display message bodies.
- This design does not protect against malware or another process already running as the mailbox owner's Unix user.

## Support and revocation limits

The Outlook REST/OWA endpoints and first-party SPA auth behavior are private and unsupported for this use. Microsoft or U of T may change or block them without notice. Use only with the owner's mailbox and at human-scale request rates. Local logout removes helper credentials but is not server-side revocation; use Microsoft's account/session controls for revocation after suspected compromise.
