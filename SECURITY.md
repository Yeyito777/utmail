# UTmail persistent OWA-session security contract

`utmail` is an unofficial, user-session-authenticated, read-only client for a user-owned University of Toronto Outlook Web mailbox.

## Authentication boundary

- The helper does not register an Entra application and does not use Microsoft Graph.
- The recommended `login --persistent` flow asks `vimbrowser-cli` to open Outlook in the named `utmail-helper` context (configurable with `UTMAIL_VIMBROWSER_CONTEXT`). The mailbox owner completes Microsoft/U of T sign-in there when required.
- vimbrowser owns that isolated persistent context and stores its Microsoft/Outlook cookies, MSAL cache entries, and other browser state. These credentials have the same broad practical sensitivity as a signed-in Outlook browser and may permit actions far beyond this CLI.
- The helper additionally stores the currently selected OWA access token and rotating first-party SPA refresh token in `session.json` so ordinary renewals use direct HTTPS without opening a browser tab.
- Microsoft gives SPA refresh tokens an approximately 24-hour hard refresh window. Rotating the token does not make that window permanent. After the hard window ends, the helper opens a transient exact Outlook tab in the same named context so Outlook can use its signed-in Microsoft cookies to obtain a new authorization grant, then closes only that tab.
- Continued unattended renewal is best-effort, not literally indefinite. Microsoft or U of T can require human reauthentication after password changes, MFA/Conditional Access events, explicit revocation, inactivity, or policy limits.
- `login --from-vimbrowser` and `login --token-stdin` remain short-lived compatibility modes. They do not enable automatic renewal or copy vimbrowser cookies.
- Credentials are never accepted in argv, printed, logged, included in errors, or emitted as JSON.
- `session.json` is protected by a mode `0700` parent directory, is mode `0600`, and is replaced atomically under a process lock. The state is not separately encrypted at rest, so compromise of the owner's Unix account can expose it. vimbrowser is independently responsible for protecting its context storage.
- `utmail logout` deletes only the helper-owned access/refresh-token state. The vimbrowser context remains and may still be signed in; logout neither revokes Microsoft server-side sessions nor signs out vimbrowser.
- Legacy releases stored browser state in `~/.local/state/utmail/browser-profile/`. Version 0.4 no longer reads or manages that Playwright profile; remove it after migration so obsolete signed-in state is not retained.

## Renewal network boundary

- Direct renewal uses a form-encoded HTTPS POST only to the exact tenant path under `https://login.microsoftonline.com/`.
- The request is fixed to Outlook Web's first-party client ID, `grant_type=refresh_token`, and `https://outlook.office.com/.default`; there is no generic OAuth or endpoint escape hatch.
- Redirects are disabled, response size is bounded, credentials remain in the request body, and OAuth response bodies are never echoed in errors.
- When the approximately 24-hour SPA refresh window ends, the named vimbrowser context may load Microsoft, U of T SSO, and Outlook pages. Outlook itself performs background authentication and application requests. The CLI does not attempt intentional mail mutation, but the stored browser session is not server-side read-only.
- Persistent credential discovery is restricted to the exact new tab ID returned by `open-context`, its exact current main-frame ID, the exact HTTPS Outlook host/path, Outlook's first-party application ID, and one unambiguous MSAL home account. Subprocess time and output are bounded, and credential-bearing frame output is never included in errors.

## Mailbox network boundary

- All direct mailbox API requests are HTTPS GET requests.
- The only allowed mailbox API origin and prefix are `https://outlook.cloud.microsoft/api/v2.0/`.
- Redirects are disabled. Pagination links must remain under the same exact origin and prefix.
- POST, PATCH, PUT, DELETE, arbitrary mailbox endpoint escape hatches, and mutation commands are absent.
- Request count, pagination, retries, response bytes, attachment bytes, and timeouts are bounded.

## Local boundary

- UTmail session/config directories are mode `0700`; structured credential files are mode `0600` and replaced atomically. vimbrowser context storage is outside UTmail's ownership and must be protected according to vimbrowser's security guidance.
- Downloads are regular files with mode `0600`, reject symlink directories, refuse overwrite without `--force`, and have a fixed size ceiling.
- Inbox/search summaries omit bodies. Only explicit `show` and `thread` commands retrieve message bodies. Their optional link extraction and compact-body processing are local transformations; extracted links, including decoded Outlook SafeLink destinations, are never fetched or followed.
- This design does not protect against malware or another process already running as the mailbox owner's Unix user.

## Support and revocation limits

The Outlook REST/OWA endpoints and first-party SPA auth behavior are private and unsupported for this use. Microsoft or U of T may change or block them without notice. Use only with the owner's mailbox and at human-scale request rates. Local logout removes helper credentials but is not server-side revocation; use Microsoft's account/session controls for revocation after suspected compromise.
