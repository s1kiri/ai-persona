# Memory

Three layers, and the difference between them is what makes a character continuous rather than a
chatbot with a costume on.

| layer | file | who writes it | how it changes |
| --- | --- | --- | --- |
| foundational | `PERSONA.md` | a human sets the core; the character refines the rest | rarely, deliberately |
| state | `memory/state.md` | the character | rewritten and compressed every session |
| entities | `memory/entities/*.md` | the character | appended, dated, linked |

## Why the middle layer is the dangerous one

State that only grows becomes the entire context window. Then every session pays for every session
before it, the character gets slower and duller as it gets older, and the cost curve is a straight
line upward with nothing at the top.

The fix is not a size limit enforced by code, although having one as a backstop is sensible. The
fix is a rule the character follows: when the state file starts feeling long, that is the signal to
compress what is already in it. Keep the current mood and goal, open threads, and lessons that will
still matter next month. Drop what happened on a particular day unless it changed something.

## Why entities are one file each

Because that is how you remember a person rather than a conversation.

A single log of everything that happened, ordered by time, is searchable and useless: to know who
somebody is you have to read the whole thing. One file per person, with dated lines inside it,
answers the question directly. The character opens `marlow.md` and knows Marlow.

Links in double brackets, written into both files, make it a graph. Open the folder in Obsidian and
it draws itself, and you can see at a glance who is central and who is peripheral, which is
information nobody wrote down deliberately.

## The language rule

Remember in one language, speak in another, and never confuse them.

A character speaking Russian should still write its memory in English, because memory is searched
and joined rather than read aloud. But names stay exactly as spelled, and direct quotes stay in the
language they were said in, because a translated quote is a paraphrase wearing quotation marks and
will eventually be quoted back to somebody as though it were their words.

## What not to remember

Anything true for the next ten minutes. Anything recomputable in a second. And anything about a
person they would be upset to find written down, unless it is the kind of thing the character is
supposed to remember, in which case write the fact and leave the judgement out.
