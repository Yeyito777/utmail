# Development checklist

## Completed in 0.4.x

- [x] Implement a private, atomic OWA access-token session store.
- [x] Validate token audience, first-party Outlook client identity, account identity, scopes, and expiry.
- [x] Delegate persistent login to an isolated named vimbrowser context through `vimbrowser-cli`.
- [x] Implement short-lived compatibility imports from an exact vimbrowser tab or stdin.
- [x] Restrict direct mailbox traffic to bounded HTTPS GET requests under the exact Outlook REST origin/prefix.
- [x] Implement `status`, `whoami`, `inbox`, `search`, `show`, and `thread`.
- [x] Implement attachment listing and private, bounded downloads.
- [x] Implement fixed-endpoint access-token renewal with rotating refresh-token storage.
- [x] Recover through the named vimbrowser context after the SPA refresh window ends.
- [x] Serialize concurrent renewal under a private process lock.
- [x] Remove only helper-owned token state on local logout and preserve vimbrowser-owned context state.
- [x] Provide stable JSON output and credential-free errors.
- [x] Document the unsupported first-party OWA authentication model and broad credential risk.
- [x] Add deterministic injected-runner tests for validation, redaction, storage, endpoint allowlisting, parsing, downloads, rotation, invalid-grant fallback, concurrency, and exact-tab vimbrowser recovery.

## Potential future work

- [ ] Add an opt-in OS keyring/encrypted-at-rest session backend.
- [ ] Add CI across supported Python versions.
- [ ] Investigate a supported Microsoft identity application path if U of T permits user-registered clients in the future.
- [ ] Add packaging/release automation after the private OWA interface proves stable enough.
