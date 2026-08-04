---
name: note-kit-anonymize
description: Identity gate at any outward-publication boundary — a git push to any repo, an agent-authored post, a hosted-platform update, or a deposit that feeds the publish path. Wraps verify_publish_anonymity.py, never a second implementation — staged bytes for a push, named files for anything else; a hit holds the outward action with its proposed anonymous replacement until the user clears it, clean content passes silently. Trigger before any push, post, publish, or outward send, or on /note-kit-anonymize with a target.
---

# anonymize

The identity gate at the outward-publication boundary. One run assembles the exact bytes about to cross — staged content for a push, the named files for anything else — scans them with the kit's anonymity instrument, and either passes silently or holds the outward action with each hit's proposed replacement until the user clears the list.

## Definitions

| term | meaning |
| ---- | ------- |
| Outward boundary | an action that puts content beyond the vault's control — a git push to any repo (not only the public template), an agent-authored post, a live-website or hosted-platform update, an external send. A deposit of foreign or transcript content into the vault feeds the publish path, so it scans on the same terms. |
| The instrument | `verify_publish_anonymity.py` (CONFIG § Helper-script automation) — the one identity scan; this skill is a caller, never a second implementation. Canaries derive at runtime and self-test before the run counts; blobs scan in UTF-8 and UTF-16 decodings; exit 0 clean, exit 2 hits. |
| Hit | one identity finding — name, email, handle, machine-absolute path, private tree name, or secret — with its proposed removal or anonymous-token replacement. |

## Procedure

- Assemble the outgoing set — exactly the bytes that will cross, never a proxy: a git push scans the staged bytes (`--staged --repo <repo>`, after staging everything the push will carry); the template store-back runs the same call under its own gate (CONFIG § Self-modification); any other boundary names its files explicitly (`--paths <files…>`).
- Run the instrument and read the exit: 0 → proceed with the outward action, saying nothing about the scan; 2 → the outward action holds.
- On a hold, surface every hit as its proposed edit — remove the line, replace it with the anonymous token the hit proposes, or record an allowlist line with its reason (`--allow-file`) — and apply only what the user clears. Re-run to exit 0 before the action crosses.
- Unattended, the hold splits ([[Stage-the-Reversible-Gate-Only-the-Irreversible]]): every reversible step completes — content built, staged, committed locally — the outward action stays unexecuted, and the hit list lands as one `<user-queue>` decision naming the action it holds (CONFIG § Queue protocol).
- The gate fires at the irreversible action the user already authorizes — the push, the post, the publish — never as a separate review pass ([[Break-the-Lethal-Trifecta-at-the-Publish-Boundary]]); an approved item naming the outward action remains the authorization (CONFIG § Self-modification).

## Bounds

The basic gate: a deterministic scan over derived identity canaries. Thorough foreign-transcript anonymization — judgment-layer scrubbing of third-party identities inside captured content — is a v2 gate and out of scope; a run that meets content needing it names the gap in its report rather than guessing.
