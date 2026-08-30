# Merced AI 0.5.0

Merced AI 0.5.0 completes profile and bot lifecycle management and adds portable, prompt-driven OAP
profile authoring. Users can generate a reviewed profile from the CLI or UI and choose project,
native-user, or universal `~/.agentprofiles` storage. Autonomous harness work emits a proposal
unless policy explicitly permits activation.

The release also replaces error-prone provider and model fields with discovered choices, improves
generation progress and source labeling, and retains the broker's loopback, token, origin, path,
credential, and subprocess authority boundaries.

Validation covers the complete Python suite, static UI syntax and API behavior, coverage threshold,
package build, Twine checks, and a clean installed-wheel smoke test.
