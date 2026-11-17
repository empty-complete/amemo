---
description: Take a document apart into amemo records with sources (atomize a file from the documents folder)
argument-hint: "<path to the file or obj:UUID> [section or pages]"
---

Take the document $ARGUMENTS apart into amemo records.

If agents are available, hand the work to the `ingester` agent of this plugin, so the text of the document stays
out of this conversation; pass it the path and the part to cover. Otherwise do the same yourself, following the
rules of the `ingester` agent and the `amemo-remember` skill. If a skill named `amemo-atomize` is installed, it
holds the rules of this base for documents — follow it first.

Without a path: ask `amemo_tasks` for an open task and take it (`amemo_task_claim`).

Report in three to five lines: how many records are written and with which statuses, what is left for the review
queue, which parts of the document are not covered and why.
