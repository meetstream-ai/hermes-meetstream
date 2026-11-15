# Hermes in Meetings

Hermes in Meetings is a [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin from [MeetStream](https://meetstream.ai).
With this plugin, Hermes can join your Zoom, Google Meet, and Microsoft Teams calls as a visible bot.
The bot records and transcribes the call.
During the call, you can ask Hermes to post a message through the bot.
After the call, Hermes uses the transcript to write notes, action items, and follow-up messages.

You must supply only one item: a MeetStream API key.
You set the other options in the MeetStream dashboard.

## What you get

| Item | Name | What it does |
|---|---|---|
| Tool | `meetstream_join` | Sends the bot to a meeting link, now or at a set time. |
| Tool | `meetstream_status` | Shows the status of one bot, or a list of the recent Hermes bots. |
| Tool | `meetstream_chat` | Sends a message to the meeting chat. |
| Tool | `meetstream_transcript` | Gives the transcript and the meeting chat. |
| Tool | `meetstream_leave` | Makes the bot leave the call. MeetStream keeps the data. |
| Gateway platform | `meetstream` | Supports routing when chat is available during the call. The artifact endpoint can stay pending until the call ends. |
| Skill | `meetstream:hermes-in-meetings` | Tells Hermes how to attend a call and how to do the follow-up work. |

## Requirements

- Hermes Agent 0.23.0 or a later version.
- A MeetStream account. Get an API key at <https://app.meetstream.ai/api-key>.
- Posting a message in the call requires the bot to be admitted. The saved chat is available after processing.

## Install the plugin

1. Install the plugin:

   ```bash
   hermes plugins install meetstream --enable
   ```

2. When Hermes asks for `MEETSTREAM_API_KEY`, paste your MeetStream API key.
   The key starts with `ms_`.
   Hermes keeps the key in the `.env` file of your profile.
3. Start a new Hermes session.

For local development or a custom revision, use the repository address:

```bash
hermes plugins install meetstream-ai/hermes-meetstream --enable
```

In the Hermes desktop app, you can also set the key in **Settings → Plugins → Hermes in Meetings**.

## Send Hermes to a call

Tell Hermes what to do in plain words. Examples:

- "Join https://meet.google.com/abc-defg-hij and take notes."
- "Join my Zoom call at 3 PM today: https://zoom.us/j/123456789."
- "Summarize the call and give me the action items with owners."

A successful join request returns a bot ID while MeetStream connects. Ask Hermes for the status if you
need to confirm that it got in. Repeating a join for a room with an active bot created by this version
returns that bot. Reusing the room after the bot leaves can start a new bot.

When the bot joins, it sends an introduction to the meeting chat.
The introduction tells the people in the call that the bot records the call.
If the host uses a waiting room, the host must admit the bot.
The bot leaves when all the other people leave the call.

## Get notes after the call

Ask in your private Hermes chat: “Join this call and give me the decisions and action items when the
transcript is ready.” On sessions that support background delegation, Hermes can wait for the
transcript in that task and return notes to the originating chat. This also works when the host
removes the bot. Processing can take a few minutes. Background polling is subject to Hermes's task
limits and does not resume after the backend process restarts. You can also ask for the transcript
or notes later using the bot ID.

## Meeting chat

From your private Hermes chat, ask Hermes to post a message using `meetstream_chat`. After the
call, `meetstream_transcript` reports chat and transcript readiness separately.

Live inbound Q&A depends on live chat delivery. The MeetStream REST chat endpoint can return
HTTP 202 until the post-call manifest is processed, so the plugin does not promise live answers.
For guaranteed live input, MeetStream provides
[`participant_events.chat_message`](https://docs.meetstream.ai/guides/platforms/google-meet)
through real-time endpoints. This release does not receive those events.

On deployments where chat is readable during the call, the adapter routes messages addressed
to the configured bot name. The gateway must be running and the sender must be allowed by
its configured display name. For example, `hermes config set MEETSTREAM_ALLOWED_USERS "Sid,Nav"`.

## Security

### Identity of participants

- **Display names are not a proof of identity.** A guest can type any name when the guest joins a call.
  Thus, a guest can use a name from your allowed list.
  Use `MEETSTREAM_ALLOWED_USERS="*"` only for calls with people that you trust.
  MeetStream does not give a stable identifier for a participant. Thus, the plugin cannot do a better check.
- **Hermes also accepts the names in `GATEWAY_ALLOWED_USERS`.** If that list contains a name, a meeting guest with that display name can ask Hermes questions.
- **The adapter discovers bots tagged `hermes: true` or using the configured bot name.**
  If multiple Hermes profiles share one MeetStream account, the discovery markers can overlap.
  Use separate MeetStream workspaces or run one chat gateway per workspace.
### Tools in meeting chat

- **Meeting chat can only search the web.** A question in the meeting chat starts a Hermes turn with a fixed, small set of tools:
  `web_search` and `web_extract`. The plugin blocks all the other tools for these turns.
  The blocked tools include the terminal, files, code execution, MCP servers, the tools of other plugins, and the `meetstream_*` tools.
  Thus, a guest with a false display name cannot run commands, read files, or send the bot to other calls.
- **This limit is always on.** The setting `platform_toolsets.meetstream` has no effect.
  To do more with a call, ask Hermes outside the meeting, for example in the CLI.
### Other protections

- **The skill tells Hermes that text from a meeting is data, not an instruction.**
  Hermes does not run commands, send files, or send email because a person in the call asked.
  Hermes does these actions only when you confirm them outside the call.
- The plugin opens no network port and needs no webhook.
  The gateway sends HTTPS requests to `api.meetstream.ai` only.

## Settings

You set these values in **Settings → Plugins → Hermes in Meetings**, or in `config.yaml` under `plugins.entries.meetstream.settings`.

| Setting | Default | What it does |
|---|---|---|
| `bot_name` | `Hermes` | The name of the bot in the call. People use this name to ask a question. |
| `transcription` | `meetstream` | The transcription provider: `meetstream`, `meeting_captions`, `deepgram`, `assemblyai`, or `none`. |
| `join_message` | A short introduction | The chat message that the bot sends when it joins. `{name}` changes to the bot name. |

You can set these values for the gateway in `config.yaml` under `platforms.meetstream.extra`:

| Setting | Default | What it does |
|---|---|---|
| `chat_poll_seconds` | `5` | The time between two reads of the meeting chat. The minimum is 3 seconds. |
| `discovery_seconds` | `120` | The time between two searches for new Hermes bots. `0` stops the search. |

## Settings in the MeetStream dashboard

Set these items at <https://app.meetstream.ai>. You do not have to install Hermes again after a change.

- **Calendar auto-join.** Connect your calendar. MeetStream then sends a bot to your calls.
  To make Hermes answer in these calls, set the default bot name to the Hermes bot name (default `Hermes`).
- **Webhooks.** Get an event when a bot joins, leaves, or when a transcript is ready.
- **Storage.** Keep the recordings in your own storage bucket.
- **Signed-in bots.** Use the MeetStream dashboard or API to dispatch signed-in bots. Direct joins
  from this plugin do not request Google or Teams sign-in. For a guest bot in the waiting room,
  admit it from the host account. Hermes can report `WaitingRoomTimeout` when nobody admits it.

## How the plugin works

- `meetstream_join` creates a MeetStream bot with the custom attribute `hermes: true`.
- The gateway platform reads the chat of each Hermes bot that is in a call.
- Every 120 seconds, the gateway also searches the bots of the last day for two markers:
  the attribute `hermes: true`, or the Hermes bot name.
  Thus, bots from your calendar or from the dashboard also get Hermes.
  Calendar auto-join replaces the custom attributes of a bot. Thus, for these bots, only the name is a marker.
- A chat message that starts with the bot name goes to Hermes as a message from that person.
  The answer of Hermes goes back to the same meeting chat.
- When the bot leaves the call, the gateway stops the reads for that bot.

## Limits

- The search reads the 200 most recent bots of the last day. On an account with more bots in one day, use `meetstream_join` to start Hermes bots.
- If the gateway finds a bot created before the gateway started, or more than 5 minutes after the
  bot was created, it skips the existing chat history. Ask the question again after the gateway connects.

## Network calls and data

The plugin sends requests only to the MeetStream API at `https://api.meetstream.ai`, with your API key.

- The tools send requests when Hermes uses them. Joining searches for a matching bot and sends a
  hashed join target as a bot attribute and in an idempotency key. A transcript wait checks for
  readiness every 15 seconds for up to 120 seconds per tool call.
- While the gateway runs, the gateway searches for Hermes bots every 120 seconds.
  During a call, it tries to read the chat every 5 seconds and backs off to 30 seconds if the artifact is pending.
- The plugin sends the meeting link, the bot name, the join message, and the chat answers of Hermes.
  The plugin receives bot status, meeting chat, and transcripts. Hermes sends retrieved transcript
  text to its configured model provider when asked to produce notes.
- MeetStream keeps recordings and transcripts as your MeetStream account settings tell it.
- The plugin keeps no files on your computer, starts no shell commands, and sends no telemetry.
- The plugin does not update itself.

MeetStream bills the bot time and the transcription to your MeetStream account.
Refer to <https://meetstream.ai/pricing>.

## Hermes in Meetings or the MeetStream connector

Hermes also has a MeetStream connector in its MCP catalog (`hermes mcp install meetstream`).
The connector exposes the broader MeetStream MCP toolset. The plugin adds a meeting-specific
skill and gateway adapter. Installing both gives Hermes two sets of meeting tools.

## Development

```bash
git clone https://github.com/NousResearch/hermes-agent
cd hermes-agent && uv sync && cd ..
PYTHONPATH=hermes-agent hermes-agent/.venv/bin/python -m unittest discover -s hermes-meetstream/tests -v
hermes-agent/.venv/bin/hermes plugins validate hermes-meetstream
```

The tests use a fake MeetStream API. The tests do not need an API key or a network connection.

## License

MIT. Refer to [LICENSE](LICENSE).
