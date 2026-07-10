# Capturing a Teams chat export

The `teams-chat-to-markdown` tool converts an HTML copy of a Teams message pane.
Teams has no "export chat" button, so you capture the HTML yourself from the browser's developer tools.
It takes about a minute.

> **The one thing that matters most:** Teams renders the chat as a *virtualized list* — only the messages currently on screen (plus a small buffer) actually exist in the page's HTML.
> Any message you have **not** scrolled past will be missing from the capture.
> So you must scroll through the whole conversation before copying.

## Steps

1. **Open the chat in a web browser.**
   Go to `teams.microsoft.com`, sign in, and open the meeting chat / conversation you want.
   (The web client is easiest; the desktop app has dev tools too but they're more fiddly to reach.)

2. **Load every message by scrolling.**
   - Scroll all the way **up** to the oldest message.
     Pause a moment at the top so the earliest messages finish loading.
   - Then scroll all the way back **down** to the newest message.
   - For a long chat, scroll in stages rather than flinging the scrollbar, so each section has time to render.

3. **Open Developer Tools.**
   Press `F12` (or `Ctrl+Shift+I`).
   Click the **Elements** tab.

4. **Find the message list.**
   In the Elements panel press `Ctrl+F` and type `chat-pane-list`.
   It will highlight a `<div id="chat-pane-list" …>` (you can also search for `message-pane-list-runway`).
   That element is the whole message list.

5. **Copy it.**
   Right-click that highlighted element → **Copy → Copy element**.
   This copies the element and everything inside it (its `outerHTML`).

6. **Save to a file.**
   Paste into a new plain-text file named `chatlog.html` (any name ending in `.html` is fine) and save it.

7. **Convert it.**
   Run `teams-chat-to-markdown.py` on that file — see the [README](README.md) for the command.
   It reports how many messages it found.

## Checking the capture is complete

After conversion, the tool prints a message count.
Compare it against roughly what you'd expect for the meeting.
Also check that the **first message** in the `.md` is genuinely the start of the conversation — if it begins partway through, the top of the chat didn't load, so scroll higher and re-capture.

## Gotchas

- **Very long chats.**
  On a very long conversation the virtualized list may drop the top rows again by the time you reach the bottom.
  If the count looks short, capture in halves (top portion, then bottom portion) into two files and convert each, or use the desktop app which sometimes keeps a larger window.
- **Timezone.**
  Timestamps come out in whatever local time your Teams client was showing; the tool derives that offset automatically.
- **Quoted replies are previews.**
  When someone replies-with-quote, Teams only stores a *truncated* preview of the quoted message (ending in `…`), so the blockquote in the output is truncated too.
  The full original is still elsewhere in the log as its own message.
