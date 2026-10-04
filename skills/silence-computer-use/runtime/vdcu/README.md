# Virtual desktop runtime

This runtime provides an isolated Windows shadow session for **Silence Computer Use**.
The GUI agent executes inside that session rather than sending input to the user's
active desktop. Files and possibly the clipboard remain shared with the host;
this is not a virtual machine or a security sandbox.

## Run

From the skill root, use `python scripts/vd_cu.py doctor`, `up`, `status`, and `down`.
See `../../SKILL.md` for the supported workflow and `../../references/api.md` for
window interaction. `run_cli.py` manages sessions and auxiliary SDK operations.

## Bundled components

- `vdcu/`: session manager, neutral authorization/preview UI, SDK bindings.
- `engine/app/`: signed native SDK/backend.
- `engine/python/`: signed interpreter required by the SDK.
- `engine/python-packages/`: optional browser/computer-use clients.
- `autolaunch.json`: starts the GUI agent **inside** the authorized session.

Keep signed binaries unchanged. Their use and redistribution remain subject to
applicable third-party rights; a local copy does not itself grant a license.

## Authorization

Before creating a password card, Windows checks the current **local** account once
with an empty credential. If accepted, creation proceeds without the card. Account-restriction error 1327
also attempts normal SDK empty authorization without a card, but is not proof of
success. Other accounts or inconclusive results require local authorization. An empty
manual submission is accepted as an input; Windows and the SDK still decide
whether authentication succeeds. A rendered frame is required before startup is
reported as successful. Failure/cancellation stops; there is no automatic retry.

No passwords are read from Credential Manager or saved by the host. Do not submit
credentials through chat. Do not change account passwords or relax Windows policy.

For details see [engine notes](docs/ENGINE-NOTES.md). Offline regressions:
`python -B -m unittest discover -s tests -v`.
