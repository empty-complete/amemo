---
description: Verify draft amemo records against their sources (the semantic review queue)
argument-hint: "[how many items]"
---

Run the amemo semantic review with the `amemo-verify` skill for up to $ARGUMENTS items; without a number — 20.

If agents are available, hand the work to the `verifier` agent of this plugin, so the text of the sources stays
out of this conversation. Compare each record only with the text of its own source fragment. When in doubt,
leave the item for a person.

Report in one line: how many confirmed, how many refuted, how many left for a person.
