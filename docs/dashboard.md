# The MemoryGate dashboard

A single-workspace console for inspecting everything MemoryGate holds and why.

## Dashboard

The dashboard is a single-workspace operating console for one agent:

- **Command Center**: system counts, signal health, recent activity, and promotion metrics.
- **Live Pipeline**: a timestamped trace of incoming data through evidence, analysis, knowledge, and write decisions.
- **Memories**: inspect, create, edit, and connect durable fact, phase, context, and watch records.
- **Entities**: browse people, projects, concepts, and linked evidence with graph navigation.
- **Memory Lab**: saved browser-session investigations. Each question is independent and read-only; inspect the exact objects supplied to the model.
- **Database**: search and inspect every object type in one table.
- **Sources & Evidence**: manage listener credentials, inspect immutable evidence, and verify ingest endpoints.
- **Episodes / Sessions / Observations / Derived Patterns**: focused views for time-bounded events, transcript archives, extracted signals, and promoted patterns.
- **Architecture**: developer-facing object, lineage, truth, and search model.
- **Settings**: keys, backups, AI runtime, and destructive operations.

## Screenshots

### Command Center

![MemoryGate command center](screenshots/overview.png)

### Memories

![MemoryGate memories](screenshots/memories.png)

### Database Inspection

Search every durable object from one table, then open an object to inspect its
metadata, history, and connected records.

![MemoryGate database inspection](screenshots/database.png)

### Entities Graph

![MemoryGate entities graph](screenshots/entities-graph.png)

### Observations

![MemoryGate observations](screenshots/observations.png)

### Derived Patterns

![MemoryGate derived patterns](screenshots/patterns.png)

### Briefing

![MemoryGate briefing](screenshots/briefing.png)

### Beliefs

![MemoryGate beliefs](screenshots/beliefs.png)

### Memory Lab

Memory Lab answers independent, read-only questions. It keeps an investigation
list only in the current browser session and exposes the exact retrieved
objects used for each answer.

![MemoryGate Memory Lab](screenshots/memory-lab.png)

### Operations and Safety

Settings keeps access controls, backups, model configuration, and destructive
reset controls together. Reset actions require the current admin key and an
explicit confirmation phrase.

![MemoryGate settings and danger zone](screenshots/settings.png)

### Transcript Detail

![MemoryGate transcript detail](screenshots/transcript-detail.png)
