# Scripts — the only way to act

A character observes and acts through these and through nothing else. It never runs the
infrastructure that runs it.

The contract is deliberately dull: a script takes plain arguments and prints a plain result. That
makes it usable from any runner, testable by hand, and readable in a transcript.

| script | what it does |
| --- | --- |
| `say.sh` | append a message to the outbox, addressed to somebody |
| `remember.sh` | append a dated line to an entity note, creating the note if needed |
| `ticket.sh` | report that something is missing or broken, to whoever maintains the world |
| `sleep.sh` | set your own next wake, in minutes |

These four are examples. Replace them with the real actions of your world; keep the shape.
