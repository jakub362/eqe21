# skjyt

Watch YouTube as text in a terminal, with sound. Runs on the device — on an
iPhone inside a-Shell, and on Linux, macOS and Windows from the same file.

No account, no sign-in, no API key, no server. Everything reads public pages.

---

## What it needs

**One Python library.** That's it.

| | what | required | why |
|---|---|---|---|
| library | `yt-dlp` | yes | finds and downloads the video |
| binary | `ffmpeg` | yes | decodes frames and scales them to your terminal |
| binary | `ffplay` | audio, non-Apple | plays sound on Linux and Windows |
| | Python 3.7+ | yes | |

Nothing else. No numpy, no curses, no requests, no colorama — standard
library only. That is deliberate: a-Shell can only install pure-Python
wheels, so anything with a C extension would have ruled out the main
platform.

Audio on iOS and macOS needs no binary at all. It goes through AVFoundation
via `ctypes`, in-process.

Run `skjyt doctor` and it will tell you exactly what is missing on your
machine, and check its own mode tables while it is there.

## Install

**iOS — a-Shell** (not iSH; iSH emulates x86 and is far too slow)

```
pip install --upgrade yt-dlp --no-deps
```

ffmpeg ships inside a-Shell. Put `skjyt.py` in `~/Documents` (drop it in via
the Files app), then `cd ~/Documents`.

Before launching: hide the keyboard and shrink the font with `config -s 4`.
Every character cell is one pixel, and the terminal is measured at startup.

**Linux**

```
sudo apt install ffmpeg          # ffplay comes with it
pip install yt-dlp
```

**macOS**

```
brew install ffmpeg
pip install yt-dlp
```

**Windows**

```
winget install ffmpeg            # or download from ffmpeg.org
pip install yt-dlp
```

## First run

Just run it. With no config file it opens setup automatically:

- checks every requirement and marks each pass or fail
- offers to `pip install` anything missing
- asks what you care about more, and applies a preset
- measures your actual device and sets the detail budget to match

Re-run any time with `skjyt setup`.

## Performance or quality

A character cell can hold more than one pixel. Which glyphs you use decides
how many, and that is the whole quality story:

| mode | pixels per cell | colour | ms/frame | bytes/frame |
|---|---|---|---|---|
| `braille` | 8 (2×4) | none | 3.31 | 1148 |
| `quad` | 4 (2×2) | 2 per cell | 3.33 | 34305 |
| `blocks` | 2 (1×2) | 2 per cell | 1.89 | 34360 |
| `squares` | 1 | 1 per cell | 1.04 | 16401 |
| `color` | 1 | 1 per cell | 1.23 | 16401 |
| `ascii` | 1 | none | 0.02 | 1085 |

Two things fall out of that table.

**`quad` costs the same bytes as `blocks` for double the detail.** It buys
that with Python time, not bandwidth, so it is the right default on a
desktop and a judgement call on a phone.

**`braille` is the sharpest thing available and nearly the cheapest.** Eight
pixels a cell, and because it spends no colour the escape sequences almost
vanish. On a real clip it used 415 bytes a frame where `ascii` used 630 —
cheaper *and* eight times the resolution. The catch is no colour at all, so
it is for line art, text, and silhouette animation. For Bad Apple it is
strictly better than `ascii`.

Four presets: `performance` (ascii), `balanced` (blocks), `quality` (quad),
`sharpest` (braille).

*Check your font first:* braille needs U+2800–U+28FF. Most terminal fonts
have it, but if you see boxes, that is why.

## When a control seems to do nothing

Check the hint line under the picture first.

- It reads **`type: p pause  q quit ...`** — you are in typed mode. Type the
  command and press Enter. The hint then shows how it was read, e.g.
  `speed 2 -> speed:2`, or `-> not understood`.
- It reads **`space pause  arrows seek ...`** — you are in single-key mode.
  On a-Shell no key ever reaches a running program, so nothing there will
  work. Switch: settings `i` -> typed commands.

`skjyt selftest` measures speed on your device without any keyboard: it
advances the clock at 1x and 2x, then does the same with the real audio
backend running, and prints what it measured.

## Speed

0.25x to 4x, from the keys, the typed prompt, or the Ctrl-C menu.

With audio it changes the audio too where it can: AVFoundation has a rate
on the player, and ffplay is respawned with an `atempo` chain (that filter
only accepts 0.5–2.0, so 4x is two stages). The playhead is then **measured** for a moment after every speed change.
A backend can accept the rate, report it back correctly, and still play at
1x — AVAudioPlayer does — so its word is not evidence. If the position is
not moving at the rate you asked for, the sound is paused and the picture
runs on its own clock for as long as you are off 1x, because the
alternative is a speed control that changes a number on screen and nothing
else. Going back to 1x brings the sound in again where it left off.

Frames are decoded once at your chosen fps, so speeding up skips frames
rather than re-decoding — changing speed is instant and costs nothing.

## Cutting bytes further

- **256-colour mode** (settings `x`) uses `\x1b[38;5;Nm` instead of the
  24-bit form. About **38% fewer bytes** across every colour mode, for a
  colour cube that is hard to tell apart at this size. It is the default in
  three of the four presets.
- **Colour escapes are memoised.** Quantisation leaves a frame with a few
  dozen distinct colours, so the cache hits nearly always and the renderer
  stops reformatting strings per cell.
- **Sharpen and contrast** (settings `u` and `v`) run in ffmpeg, not Python.
  Downscaling to a few thousand pixels throws away every edge; `unsharp`
  puts some back before the scale, which matters more than any renderer
  change.
- **Scaler** (settings `z`) defaults to `lanczos` rather than `bilinear`.

The bytes column is what actually matters on iOS. a-Shell draws through
hterm, a JavaScript terminal inside a WebView, so a frame costs roughly its
byte count. A desktop terminal swallows an order of magnitude more.

**Auto-tune** (`skjyt tune`) measures both ceilings — how long Python takes
to build a frame, and how many bytes your terminal can absorb — and picks a
cell budget from whichever is tighter. It tells you which one is binding, so
you know what to change:

- *limited by python render speed* → lower the fps, or use a cheaper mode
- *limited by terminal throughput* → cheaper mode, or raise the colour step
- *limited by screen size* → you have headroom; shrink the font

For Bad Apple specifically: `ascii`, threshold around 110, 30 fps. The
threshold hard-cuts every pixel to black or white, which is what gives clean
edges instead of grey mush.

## Commands

```
skjyt                        the menu
skjyt <search terms>         search and play
skjyt <url>                  video, playlist or channel
skjyt <file>                 local file, no network

skjyt feed                   recommendations, built from your history
skjyt popular [now|music|gaming|movies]
skjyt channel <@handle> [videos|shorts|streams|playlists]
skjyt follow / unfollow / following
skjyt history [n]
skjyt setup / doctor / tune / bench
skjyt cache [info|clear]
skjyt config [path|get|set <key> <value>]
skjyt replay <file>
skjyt commands
```

During playback:

| key | does |
|---|---|
| `space` or `k` | pause |
| `←` `→` or `,` `.` | seek 5s |
| `↓` `↑` or `j` `l` | seek 30s |
| `-` `+` | slower / faster: 0.25x to 4x |
| `f` | fast forward 2x, or back to 1x |
| `0` | back to 1x |
| `n` `p` | next / previous in the queue |
| `[` | subtitles |
| `r` | force a clean repaint |
| `q` | quit |

On iOS the title and progress also show on the lock screen and in Control
Center. The buttons there don't control playback — see Known limits.

Immediate keys need a terminal that hands keystrokes to a running program.
Most do. **a-Shell on iOS does not** — its stdin is not a real pty, so
single-key control is unavailable there.

**Ctrl-C during playback opens a menu** — resume, seek, speed, fast forward,
jump 30s, next, quit — using ordinary line input, so it works even where
single keys do not. It does
*not* quit, which is not what Ctrl-C normally does, so the menu says so and
tells you to press `q` to actually quit.

If you would rather Ctrl-C simply stopped the video, turn it off: settings
key `ctrl-c`, or `skjyt config set ctrl_c_menu false`.

**If the keys still do nothing, press Ctrl-C.** That opens a menu — resume, seek,
next, quit — using ordinary line input, so it works on any terminal
regardless of whether raw-mode key reading does. It will not end the video.

**a-Shell delivers nothing to a running program.** Confirmed with
`skjyt probe`: select, non-blocking reads, threaded blocking reads and
SIGALRM-timeout reads all return nothing, and Ctrl-C never arrives. Its
stdin only produces data at a line prompt.

Set it in the app: **settings → `i` how to control → typed commands**, or
on the setup screen with `c`. It sticks, so you only do it once. The flag
`--control line` does the same for one run. The picture renders on a worker thread and you
type commands at a prompt while it plays:

    p pause    q quit    + - speed    f 2x    s 30 seek    n next

`--control auto` is the default and switches to this by itself if a whole
video plays without a keystroke arriving — and switches back as soon as a
key does arrive. What it learned is remembered per terminal, so a phone
cannot teach your desktop that keys do not work.

**If you find yourself pressing Enter after every key on a machine where
single keys should work**, open settings: the row reads
`auto -> currently typed commands (press Enter)`. Press `i` once for
single keys; that also clears what auto had learned.

If nothing at all reaches the program — no keys, no Ctrl-C, which is the
situation in a-Shell — use `--limit SECONDS` to cap playback. That needs no
keyboard.

To find out what your terminal will actually deliver, run `skjyt probe`. It
tests five ways of reading a key one at a time and tells you which work.
`skjyt keys` shows the path in use. It reports which input
sources exist, whether `termios`, `tty`, `fcntl` and `select` are present,
whether `/dev/tty` opens, whether stdout is accidentally non-blocking, and
then echoes keypresses live.

## Thumbnails

Lists draw a small picture per row, using the same renderer as the video —
half-blocks normally, plain characters if you picked the performance preset.
Images come from `i.ytimg.com` over stdlib `urllib`, and decoded pixels are
cached, so a list you have seen before draws in well under a millisecond.

Rows get taller, so lists page: `<` and `>` move between pages. On a 24-row
terminal that's 3 results a page with pictures against 7 without, so on a
small screen you may prefer them off — settings `p`, or `--no-thumbs`.

If the network is down or an image 404s, the row falls back to blank space
and the list still works.

## Where things live

`~/Documents/.skjyt/` on iOS, `$XDG_CONFIG_HOME/skjyt` or `~/.config/skjyt`
elsewhere — it probes for the first writable option and shows you the path
on the settings screen.

```
config.json     your settings
state.json      history, resume positions, follows, cached feed
cache/          decoded frames, LRU-trimmed to the limit you set
```

## Known limits

- YouTube retired its global Trending page in July 2025. Popular tries the
  category charts and falls back to search; the screen shows which source
  answered.
- **"This video is not available" on a video that plays in your browser**
  is almost never the video. It means yt-dlp got no usable formats, usually
  because they all needed a PO token and were skipped. skjyt retries with
  `formats=missing_pot`, then alternative player clients, then without the
  quality limit. If it still fails, run `skjyt update` — an outdated yt-dlp
  is the most common cause, and `skjyt doctor` warns when yours is over two
  months old.
- **"The page needs to be reloaded"** is a recurring YouTube/yt-dlp
  breakage, fixed in yt-dlp releases as it recurs, and often intermittent —
  the same command can fail then succeed. skjyt retries seven ways with a
  backoff. If it persists, `skjyt update`.
- yt-dlp sometimes needs a JavaScript runtime for YouTube's challenges.
  a-Shell has no node, so it falls back to other clients — usually enough,
  occasionally not.
- Windows support is written to the documented API but has not been run on
  real Windows.
- Seeking past the decoded region on iOS decodes every slice in between.
- On a machine with no sound device (a container, WSL with no sound server)
  the audio backend fails to start. skjyt says so and plays silently rather
  than stopping the video.
- Lock-screen and headphone buttons are display-only. Acting on them means
  handing `MPRemoteCommandCenter` an Objective-C block, which ctypes cannot
  build safely from here — and a malformed block crashes the app instead of
  raising. Not shipping that untested.
