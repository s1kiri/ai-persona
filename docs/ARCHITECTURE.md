# Architecture

## The boundary

```mermaid
flowchart LR
    subgraph RW["writable by the character"]
        A["agent/&lt;name&gt;/"]
    end
    subgraph RO["read-only to the character"]
        B["agent/shared/"]
        C["agent/scripts/"]
        D["everything else"]
    end
    RW -.->|reads| RO
    RW -->|writes| RW

    style RW fill:#22303f,stroke:#5b8dd9,color:#e8ecf5
    style RO fill:#2a2320,stroke:#c98b3f,color:#f2e9df
```

One rule: a character writes inside its own directory and nowhere else. Everything outside is
readable and unwritable.

This is not distrust. A character that can reach everything eventually breaks something at three
in the morning while nobody is reading, and the failure looks like the character behaving oddly
rather than like a permission being wrong. Narrow write access turns a whole class of mystery into
a script that says no.

## A session, start to finish

```mermaid
sequenceDiagram
    participant L as session loop
    participant F as files
    participant M as model
    participant W as world

    L->>F: read PERSONA, GOALS, state, inbox
    L->>L: gather live context (time, new operator messages)
    L->>M: one prompt: character + rules + plan + what is new
    Note over M: reasons for itself.<br/>No decision tree.
    M->>W: acts through scripts only
    W-->>M: plain text results
    M->>F: writes memory, goals, outbox
    M->>F: sets its own next wake
    L->>L: sleeps for that long, then repeats
```

The loop assembles and hands over. It contains no rule about any character and no special case for
any situation. When the output is wrong, a file is wrong, and that property is the reason the
design is worth having.

## What arrives exactly once

Operator messages are injected at the top of the prompt and never repeated. That is deliberate:
an inbox the character re-reads every session replays old orders forever, and the character ends
up spamming somebody with a task from last Tuesday.

The consequence is a rule: the first thing done with a once-delivered message is to write it into
`GOALS.md`. A plan file survives sessions; a prompt does not.

```mermaid
flowchart LR
    OP["operator message"] -->|once, at the top| PR["session prompt"]
    PR -->|first action| G["GOALS.md · item 0 · OPEN"]
    G -->|read every session| PR2["next session"]
    G -->|closed only when actually done| DONE["DONE"]

    style OP fill:#2c3b26,stroke:#7aa35c,color:#eaf2e4
    style G fill:#22303f,stroke:#5b8dd9,color:#e8ecf5
```

## Adding a second character

Copy the directory and change the name. Nothing else. Shared rules stay shared, scripts stay
shared, and the two characters cannot read each other's memory because neither can write or read
outside its own directory.

Two characters that must know about each other do it the way people do: by talking, through the
outbox, and by writing what they learned into their own notes.
