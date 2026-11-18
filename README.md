# Hermes in Meetings

Hermes in Meetings is a [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin from [MeetStream](https://meetstream.ai).
With this plugin, Hermes can join your Zoom, Google Meet, and Microsoft Teams calls as a visible bot.
The bot records and transcribes the call.
During the call, the people in the call can ask Hermes a question in the meeting chat, and Hermes answers in the chat.
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
| Gateway platform | `meetstream` | Reads the meeting chat and sends the answers of Hermes to the chat. In meeting chat, Hermes can only search the web. |
| Skill | `meetstream:hermes-in-meetings` | Tells Hermes how to attend a call and how to do the follow-up work. |

## Requirements

- Hermes Agent 0.23.0 or a later version.
- A MeetStream account. Get an API key at <https://app.meetstream.ai/api-key>.
- For answers in the meeting chat: the Hermes gateway must run during the call.

## Install the plugin

1. Install the plugin:

   ```bash
   hermes plugins install meetstream --enable
   ```

2. When Hermes asks for `MEETSTREAM_API_KEY`, paste your MeetStream API key.
   The key starts with `ms_`.
   Hermes keeps the key in the `.env` file of your profile.
3. Start a new Hermes session.

Before the plugin is in the Hermes catalog, use the repository address:

```bash
hermes plugins install meetstream-ai/hermes-meetstream --enable
```

In the Hermes desktop app, you can also set the key in **Settings → Plugins → Hermes in Meetings**.

## Send Hermes to a call

Tell Hermes what to do in plain words. Examples:

- "Join https://meet.google.com/abc-defg-hij and take notes."
- "Join my Zoom call at 3 PM today: https://zoom.us/j/123456789."
- "Summarize the call and give me the action items with owners."

When the bot joins, it sends an introduction to the meeting chat.
The introduction tells the people in the call that the bot records the call.
If the host uses a waiting room, the host must admit the bot.
The bot leaves when all the other people leave the call.

## Ask Hermes questions during the call

Do these steps one time:

1. Write the names of the people who can ask questions in `MEETSTREAM_ALLOWED_USERS`.
   Use the display names that the call shows, with a comma between two names.

   ```bash
   hermes config set MEETSTREAM_ALLOWED_USERS "Sid Sivasubramanian,Nav"
   ```

2. Start the gateway:

   ```bash
   hermes gateway
   ```

In the call, start a chat message with the bot name.
Examples: "Hermes, what did we decide about the price?" or "@Hermes, give a summary of the last 10 minutes of chat."
Hermes answers in the chat.
Hermes does not hear the call during the call.
Hermes reads only the chat messages that start with its name, or that contain "@" and its name.

If `MEETSTREAM_ALLOWED_USERS` is empty, Hermes does not answer in the chat.
Hermes continues to record the call and to take notes.

## Security

### Identity of participants

- **Display names are not a proof of identity.** A guest can type any name when the guest joins a call.
  Thus, a guest can use a name from your allowed list.
  Use `MEETSTREAM_ALLOWED_USERS="*"` only for calls with people that you trust.
  MeetStream does not give a stable identifier for a participant. Thus, the plugin cannot do a better check.
- **Hermes also accepts the names in `GATEWAY_ALLOWED_USERS`.** If that list contains a name, a meeting guest with that display name can ask Hermes questions.
- **Hermes answers only in the calls of its own bots.** The gateway does not send messages to other bots of your account.
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
| `transcription` | `meetstream` | The transcription provider: `meetstream`, `meeting_captions`, `deepgram`, `assemblyai`, or `none`. `meetstream` adds $0.10 for each hour. For `deepgram` and `assemblyai`, save your own provider key in the MeetStream dashboard. |
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
- **Signed-in bots.** Let the bot join calls that admit only signed-in users.

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
- If the gateway finds a bot more than 5 minutes after the bot started, Hermes does not answer the older chat messages.
  The same occurs after a restart of the gateway. Ask the question again.

## Network calls and data

The plugin sends requests only to the MeetStream API at `https://api.meetstream.ai`, with your API key.

- The tools send a request only when Hermes uses a tool.
- While the gateway runs, the gateway searches for Hermes bots every 120 seconds.
  During a call, it reads the meeting chat every 5 seconds.
- The plugin sends the meeting link, the bot name, the join message, and the chat answers of Hermes.
  The plugin receives bot status, meeting chat, and transcripts.
- MeetStream keeps recordings and transcripts as your MeetStream account settings tell it.
- The plugin keeps no files on your computer, starts no shell commands, and sends no telemetry.
- The plugin does not update itself.

MeetStream bills the bot time and the transcription to your MeetStream account from prepaid credit.
Refer to <https://meetstream.ai/pricing>.
If the account has no credit, `meetstream_join` tells Hermes to give you the top-up link: <https://app.meetstream.ai/billing>.

## Hermes in Meetings or the MeetStream connector

Hermes also has a MeetStream connector in its MCP catalog (`hermes mcp install meetstream`).
The connector gives all the tools of the MeetStream MCP server, but no answers in the meeting chat.
Install one of the two, not the two together. If you install the two, Hermes gets two sets of meeting tools.

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
