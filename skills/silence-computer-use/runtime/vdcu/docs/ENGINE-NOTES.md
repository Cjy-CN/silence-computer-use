# Engine and authorization notes

The default `shadow` engine uses the bundled signed SDK and interpreter to create
an independent Windows session. The `stealth` engine uses a hidden Win32 desktop
and window messages instead; it has different compatibility/input semantics and
is not a substitute for the shadow session's Kimi CU agent.

## Authentication

`authorization.py` verifies an empty secret once via local interactive
[LogonUserW](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-logonuserw).
It closes the returned token without impersonating the account or using the token
to start processes. An accepted empty local credential suppresses the card. Error 1327 means account
restrictions, not necessarily a non-empty password; it also follows one card-free
SDK attempt, but is never treated as authentication success. Only a rendered frame
proves the SDK session is usable.
A PIN is not necessarily a Windows account password. Domain/online identities or
other policy errors can be inconclusive and require the local card instead.

A failed empty check can count as a failed login for a password-protected account.
There are no automatic repeated checks. Windows policy can restrict blank-password
logons; this runtime does not change it. See Microsoft's
[blank-password policy](https://learn.microsoft.com/en-us/previous-versions/windows/it-pro/windows-10/security/threat-protection/security-policy-settings/accounts-limit-local-account-use-of-blank-passwords-to-console-logon-only).

An explicit empty password is encoded as a non-null empty UTF-8 string to the SDK.
A null pointer remains distinct and must not substitute for the empty credential.
A session GUID alone is not proof of successful authentication; startup waits for
a rendered desktop frame and stops if creation/rendering fails.

## Paths

Normally the complete bundled `engine/` is used. Explicit overrides:
`VDCU_SHADOW_APP_DIR` and `VDCU_SHADOW_SANDBOX_PYTHON`. To construct a pack from
local sources, specify `engine-pack --from <app-dir> --interpreter <python-dir>`
or `VDCU_SOURCE_APP_DIR` / `VDCU_SOURCE_PYTHON_DIR`. No unrelated product
installation is discovered automatically.

## Coordinates and limits

The SDK uses normalized coordinates, while Kimi CU window actions use screenshot
pixel coordinates. Do not mix the two APIs. SDK pointer operations require a
recent observation. GUI task completion must be verified independently of RPC
acceptance. The SDK's own native resources/metadata are not edited: modifying
signed components would invalidate their integrity and can break startup.
