# Session repair

A session is broken when every request fails the same way. The provider rejects the request (HTTP 400), so sending it again fails again. This page says what Freyja fixes by itself, what `/repair` does, and how to fix a session's history by hand when neither helps.

## What Freyja fixes by itself

Before each turn and each request, Freyja fixes the problems it knows in the model's copy of the history:

- It closes tool calls that have no result.
- It shrinks images over the size limit, and resizes images to the per-side limit for the number of images in the request.

Other guards stop a problem where it starts, so it never reaches the history:

- **Tool names.** `ToolRegistry.register` refuses a name outside `^[a-zA-Z0-9_-]{1,64}$`. Providers reject a request that lists such a tool, so one bad name used to make every request in the session fail. Now registration fails, once, with an error that names the tool.
- **Failed scheduled runs.** A scheduled run that nothing answered takes its prompt back out of the session's history. The run record keeps the prompt and the error. Without this, each failed run added its prompt (about 3k tokens) to the history.
- **Failing jobs.** A scheduled job stops after 3 failed runs in a row (status `disabled_error`) and posts the last error to its sinks. Failed runs deliver nothing, so without this a broken job fails silently until it reaches `max_fires`.
- **Model on reload.** A session reloaded from disk keeps the model and reasoning level saved in its transcript, unless the caller asks for another. Before, a Slack thread came back on the gateway's default model after every restart.

## `/repair`

`/repair` runs the history fixes above now, saves the transcript and reports what changed in the chat. Use it when a session is stuck anyway. It writes the old transcript to `<session>.transcript.pre-repair-<time>.json.gz` first.

`/repair images` also replaces every image in the model's copy with a text marker. This clears image rejections that no fix recognizes yet. The chat you see and the image files stay as they are.

`/repair` does not change the tool list, the saved model, or messages that should not be in the history. If the error names a tool (`tools.27.custom.name: ...` from Anthropic, `Invalid 'tools[27].name'` from OpenAI), the history is fine and `/repair` cannot help. That is a bug in Freyja.

## Fixing a transcript by hand

Do this only when the history itself is wrong in a way no fix knows: messages that should not be there, or a wrong saved model.

### The file

`~/.freyja/sessions/<session id>.transcript.json`, where the session id has `:` and other unsafe characters replaced. It holds:

- `version`: always 1.
- `metadata`: `model_id`, `reasoning_level`, `coordination_strategy`, and for Slack `last_inbound_platform_ts`. A session reloaded from disk uses the saved model and reasoning level.
- `transcript.entries`: in order. Each entry has an `id`, a `parent_id` (the entry before it) and a `timestamp`. It also has a `message` (`role`, `content`), or for a compaction marker, `is_compaction` and `compaction_summary`.
- `transcript.head_id`: the id of the last entry.

### Who holds the session

The desktop app's bridge and the Slack gateway each keep their own copy of every session they have loaded. Each writes its copy to disk at the end of every turn, after `/repair`, and after a compaction. Neither writes on shutdown. So the next turn in either process overwrites an edit made on disk.

### Steps

1. Do not send anything in the session until step 5 is done.
2. Back up the file: gzip it next to the original. Never delete the original.
3. Remove the entries you want gone. For each remaining entry whose `parent_id` points at a removed entry, set it to the id of the entry now before it. Set `head_id` to the id of the last entry.
4. If the saved model is wrong, set `metadata.model_id` and `metadata.reasoning_level`.
5. Make every process that holds the session reload it from disk. Restarting them works: a rebuild restarts the desktop app and the gateway. A session that is loaded and idle also reloads when you change its model, reasoning level or mode. In Slack, send a message with `--model <id>`; in the desktop app, use the model picker. A change to the same value does nothing.
6. Check it: `TranscriptManager.from_dict(data["transcript"]).get_messages()` must load. The roles should read as a normal conversation.

### Example

A self-paced job had invalid loop-tool names, so every run failed before reaching the model. Over six hours, 180 identical run prompts went into a Slack thread's history, about 540k tokens. A gateway restart had also moved the thread to the default model. The repair:

- removed the run prompts and two plain retries of the operator's last message;
- re-linked the entries;
- saved the model the operator had chosen;
- made the gateway restart before the next message.

The guards above now prevent each part of this.
