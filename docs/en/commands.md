# Command reference

| Command | Purpose |
| --- | --- |
| `/lmem status` | Show memory status |
| `/lmem search <query> [k]` | Search long-term memory |
| `/lmem forget <id>` | Delete a Timeline memory |
| `/lmem summarize` | Summarize pending messages in the current session |
| `/lmem rebuild-index` | Rebuild the Timeline vector index |
| `/lmem rebuild-graph` | Rebuild compatible graph-derived data |
| `/lmem reset` | Reset current-session memory context |
| `/lmem cleanup [preview\|exec]` | Preview or remove old injected memory blocks |
| `/lmem webui` | Show WebUI entry information |
| `/lmem help` | Show command help |

Topic construction and governance remain WebUI operations because they require memory-space selection, preview, progress, and confirmation.
