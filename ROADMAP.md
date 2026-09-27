# skjyt — roadmap

`[x]` done · `[>]` in progress · `[~]` partial · `[-]` deliberately not doing

| | count |
|---|---|
| done | 57 |
| partial | 2 |
| not doing | 3 |
| open | 1 |

---

## Done

- [x] **63. Auto-learning was sticky, one-way, and invisible.** On PC every
  key needed an Enter afterwards - because the terminal was in typed mode,
  decided automatically during the period when keys genuinely were not
  arriving, and never revisited.
  Three problems, all fixed:
  - **One-way.** It learned "keys never arrive" and had no path back. Now
    the first keystroke that does arrive flips it straight back to keys.
  - **Not scoped to anything.** The learned value was a bare string in
    shared state. It is now keyed by `sys.platform` plus `TERM`, so what a
    phone learns cannot follow a desktop, and an old flat value is ignored.
  - **Invisible.** The settings row said "auto" and nothing else, so the
    actual behaviour was unknowable. It now reads
    `auto -> currently typed commands (press Enter)`, and choosing a mode
    explicitly clears what auto had learned so the old decision cannot
    return.
  `play()` returns whether any key arrived, which is what makes the flip
  back possible; all three call sites updated for the new return shape.

- [x] **62. "Seek doesn't do anything, or maybe I'm using it wrong."** The
  second half was right, and that is a design failure not a user error:
  `s 30` meant *go to* 0:30, which on a video already past that point looks
  exactly like nothing happening.
  Seeking now takes whatever is reasonable: `s 90`, `s 1:30`, `90`, `1:30`,
  `s +30`, `+30`, `s -15`, `>>`, `<<`, and a bare `s` for +10s. Timestamps
  parse as mm:ss and h:mm:ss. The hint shows how each command was read -
  `s 90 -> seek to 1:30` - so a misunderstanding is visible instead of
  looking like a dead control.
  Also fixed: the loose-character fallback scanned whole words, so a typo
  like "banana" became two "next" commands and skipped two videos. It only
  applies to input of three characters or fewer now.
  Every form measured against the real program: `s 20` and `20` reach 0:20,
  `0:25` reaches 0:25, `s +10` goes 0:01 to 0:11, bare `s` goes 0:01 to
  0:11, and `>>` on a 30s clip correctly runs off the end.

- [x] **61. Fast forward muted the audio; seek did nothing.** One root
  cause and one new guard.
  **The mute was a real Apple requirement I had missed:** `enableRate` must
  be set *before* `prepareToPlay`. Setting it later, at the moment the
  speed changes, is accepted and reported back correctly and does nothing -
  so the rate never applied, and the speed check added last round then
  correctly concluded the audio was not keeping up and paused it. Moved to
  right after `initWithContentsOfURL:`, before `prepareToPlay`.
  **Seek gets the same treatment as speed.** A backend that ignores
  `setCurrentTime:` leaves the clock exactly where it was, and since the
  audio is the clock, nothing moves at all - which is what "seek is broken"
  looks like. `Clock.check()` now verifies a seek too: half a second later,
  if the playhead is not near where it was sent, the audio is paused and
  the picture continues from the intended position on the wall clock.
  Tested with stand-in backends that honour and ignore a seek: the honest
  one is untouched, the other is detected and corrected in 0.5s.
  Also: a seek or a pause now clears any pending speed check, so a jump in
  the playhead cannot be mistaken for the audio racing and mute it.
  On PC every seek form measured correctly - `seek 25` lands at 0:25,
  `s 20` then `s -5` gives 0:20 then 0:16, arrows step 5s each way.

- [x] **60. "The bar shows the speed but the picture is the same."** That
  description named the bug exactly. The video takes its position from the
  audio player, and `AVAudioPlayer` will accept `enableRate`, accept
  `setRate:`, **report the value back correctly**, and still play at 1x -
  so the read-back check added in the previous round passed while nothing
  changed. Trusting the backend's word was the wrong test.
  `Clock.check()` runs every frame: after a speed change it samples the
  playhead for 0.6s and compares the observed rate against the requested
  one. If the audio is not keeping up it is paused, kept for later, and the
  picture continues on the wall clock - which is the only way the speed
  control means anything. Returning to 1x brings the sound back at the
  right position.
  Also fixed: `CommandSound.set_rate` set `self.rate` before reading
  `self.time()`, and time() scales by rate - so changing speed jumped the
  position forward by however long ffplay had been running.
  Measured on the real program: 5 seconds of wall time reaches 4s of video
  at 1x and 15s at 3x.

- [x] **59. Two tools instead of a third guess at speed.** Speed reported
  as not working on iOS twice more after the audio fix, and there is still
  no way to reproduce that device here.
  - `skjyt selftest` measures speed on the device with **no keyboard
    involved**: it advances the clock at 1x and 2x and prints what it
    measured, then makes a test tone with ffmpeg, starts the real audio
    backend, and does the same again - reporting whether the backend
    followed the rate change or was paused. Numbers from the device beat
    another hypothesis from here.
  - Typed commands now echo how they were read, held on screen for four
    seconds: `speed 2 -> speed:2`, or `blah -> not understood`. That
    separates "the command did nothing" from "the command was misread",
    which no amount of staring at the screen could distinguish before.
  Worth stating plainly in the docs: **speed keys cannot work in keys mode
  on a-Shell**, because no key ever arrives. If the hint line reads
  "space pause / arrows seek" rather than "type:", the controls are the
  wrong mode for that terminal.

- [x] **58. Speed appeared to do nothing.** With audio playing, the video
  takes its position from the audio player. So when a backend silently
  ignored a rate change, the bar said 2x and everything carried on at 1x -
  the number changed and nothing else did.
  `AVAudioPlayer.set_rate` now reads the rate back after setting it and
  returns whether it stuck. If it did not, `Clock.set_rate` pauses the
  audio, keeps a handle to it, and runs the picture on the wall clock at
  the chosen speed; returning to 1x seeks the audio to the current position
  and resumes it. A one-line notice says the audio cannot change speed
  rather than leaving it a mystery.
  Verified with stand-in backends that do and do not honour a rate change,
  and end to end: typed `speed 3` reaches 0:12 in about four seconds, and
  `+` steps 1.25x, 1.5x, 2x, 3x.

- [x] **57. The general flakiness: five separate causes.** "Sometimes there
  are 2 videos and one like a picture" and "the controls sometimes don't
  work" turned out to be five independent bugs, all found by driving the
  real program:
  1. **Scroll region leaked.** Typed mode sets one; `screen()` never
     released it, so every menu after a typed-mode video rendered inside a
     two-row strip - which is what "two videos, one like a picture" was.
     `screen()` and `restore_terminal()` now emit `\x1b[r`, playback
     releases it unconditionally rather than only in typed mode, and
     leaving a video clears the screen.
  2. **The reader thread stole the menu's first keypress.** A blocking read
     cannot be cancelled, so the thread was still holding stdin when the
     menu prompt began. The whole thread is gone: keys are polled on the
     main thread with select plus a momentarily non-blocking read, which
     does the same job with nothing to clean up. This is the single
     biggest cause of "the controls sometimes don't work".
  3. **Typed commands leaked into the next video.** The pending-command
     queue was not cleared between videos, so a leftover `q` quit the next
     one the instant it started.
  4. **A bare Enter at the menu opened the feed.** On a phone, stray
     Enters are constant. It is a no-op now.
  5. **"finished - press Enter" printed even after quitting**, so the extra
     Enter fell through onto the next screen.
  Also: the menu is too tall for a phone with the keyboard up, so under 22
  rows the banner collapses to one line, descriptions are dropped, and the
  rarely-used entries fold onto one row - 16 lines down to 11.
  And the test harness itself was lying: it matched triggers against
  cumulative output, so every step after the first fired immediately. Fixed
  to match only output since the previous step.
  Verified: three videos back to back through the menu, in both control
  modes, all play.

- [x] **56. Typed mode rendered on top of what you were typing.** The
  screenshot showed it: two progress bars, a wrapped hint, and the typed
  `p` echoed into the middle of the picture. Typing and rendering were
  both writing to the same screen with no arrangement between them.
  Three fixes, all standard terminal handling that was simply missing:
  - **A scroll region.** `\x1b[{prompt};{rows}r` confines scrolling to the
    bottom strip, so pressing Enter no longer pushes the picture up. That
    scrolling was what produced the second, stale progress bar.
  - **Cursor save and restore around every frame.** `\x1b7` … `\x1b8` puts
    the cursor back in the middle of a half-typed line after each redraw,
    instead of leaving it wherever the renderer finished.
  - **Rows reserved for the strip.** `grid_for` takes a `reserve` argument;
    typed mode asks for two more rows, because the old maths could put the
    prompt row past the bottom of the screen and make the scroll region
    invalid.
  Also removed the extra printed banner in `play_with_prompt` - printing
  scrolled the screen, which is what duplicated the bar in the first place.
  Verified against the real program: one progress bar, pause/resume/quit,
  `speed 2`, `s 20`, `f` all work, and keys mode is unaffected.

- [x] **55. Typed-command playback, for terminals that deliver nothing.**
  `skjyt probe` on a-Shell answered it: select+read, O_NONBLOCK,
  threaded blocking read and SIGALRM-timeout read **all return nothing**,
  and Ctrl-C never arrives. Its stdin only produces data when the program
  is sitting at a line prompt. So no amount of cleverness in the reader was
  ever going to work.
  `--control line` inverts the structure: the picture renders on a worker
  thread and the main thread waits in `input()`, which is the one thing
  that terminal supports. Typed commands (`p`, `q`, `+`, `-`, `f`, `0`,
  `s 30`, `speed 1.75`, `n`) go through the same handler as the keys.
  Set from the UI, not just the flag: **settings `i`** and **setup `c`**,
  both cycling auto / single keys / typed commands, and it persists - which
  matters because on a phone the flag is the least convenient way to reach
  anything. The passive doctor line was renamed "key input" so two
  different things are not both called "controls".
  `--control auto` is the default: keys, and if a whole video plays without
  one keystroke arriving, it records that in state and uses the typed
  prompt next time. Learned per terminal, not guessed.
  Three bugs found getting there, all caught by driving the real program:
  the command queue was named `COMMANDS`, which collided with the CLI help
  text defined later in the file and was silently a string by the time it
  was used; in line mode the reader thread and `input()` both read stdin
  and one swallowed the other's line, so line mode now starts no reader at
  all; and `signal.signal()` may only be called from the main thread, so
  the restore in `play()`'s `finally` raised on the worker and destroyed
  the whole exit path.
  Also fixed: `skjyt probe` ran the threaded-read test before `input()`,
  and since that thread cannot be cancelled it stayed blocked on stdin and
  ate the `input()` test - which is what left the probe stuck on screen.
  It runs last now, with a note that it leaves a reader behind.

- [x] **54. No way out of a video on a-Shell.** The screenshot showed the
  hint already admitting keys were not arriving — and Ctrl-C does not reach
  the program there either, so a video could not be stopped at all.
  - `--limit SECONDS` stops a video with no keyboard involved. It is the
    guaranteed exit while the input question is unresolved.
  - `skjyt probe` tests five ways of reading a key **in isolation, on the
    main thread**: select+read, O_NONBLOCK loop, threaded blocking read,
    blocking read with a SIGALRM timeout, and `input()` — plus whether
    Ctrl-C is delivered. Four rounds of fixing this by hypothesis failed
    because a-Shell reports every capability as present and then delivers
    nothing; this reports results instead of capabilities.
  - `Keys.drain()` now also tries the main thread (select, then a read made
    non-blocking only for the instant it takes) whenever the reader thread
    has never produced anything. Verified it never leaves stdout
    non-blocking, which is what broke the desktop before.
  - The hint line is clipped to the terminal width; it was wrapping and
    leaving the previous hint visible underneath, which is what made the
    screenshot show two conflicting lines.

- [x] **53. Playback speed, 0.25x to 4x.** Keys `-`, `+`, `0` and `f`
  during playback; `+`, `-`, `x` (type a number), `f`, `<<` and `>>` in the
  Ctrl-C menu; current speed shown on the progress bar whenever it is not
  1x.
  The clock is rate-aware rather than a stopwatch: with audio the player's
  own position already runs at the chosen rate so the video follows for
  free, and without audio the wall clock is scaled, re-anchoring on every
  change so the position never jumps. `AVAudioPlayer` needs `enableRate`
  set before `rate` does anything, and `rate` is a float — the wrong ctypes
  signature there passes garbage on arm64. ffplay cannot change speed
  live, so it is respawned with an `atempo` chain; that filter only accepts
  0.5–2.0, so 4x becomes two stages.
  Found and fixed on the way: pausing at any speed other than 1x reset the
  position to zero, because `_anchor()` read `time()` after the paused flag
  was set and got the stale value.

- [x] **51. The TUI became uncontrollable on the phone — my regression.**
  `Keys.__init__` started a stdin reader thread, and `requirements()`
  constructs a `Keys` to report the input path. So merely opening setup or
  running doctor left a thread blocked on stdin, and it swallowed every
  menu keystroke from then on. On first run, setup opens automatically —
  which is why the whole TUI went dead on a-Shell, where it had always
  worked before.
  Constructing `Keys` no longer touches stdin; only `enable()` starts the
  reader, only playback calls `enable()`, and the reader is retired and
  dropped when playback ends. `requirements()` now reports through a
  passive `input_report()` that checks `os.isatty` and imports, and starts
  nothing.
  Verified in a real terminal: first-run setup, out to the menu, into
  settings, back, into history, back, and quit — every keystroke landing.

- [x] **52. Ctrl-C menu: kept, but honest and optional.** It is a
  deliberate surprise, since Ctrl-C normally kills a program, so it now
  opens with a warning saying exactly that and how to really quit. New
  `ctrl_c_menu` setting (settings key `ctrl-c`); when off, Ctrl-C just
  stops the video. Both states verified end to end, including that the
  setting persists.

- [x] **50. Controls, and the discipline that finally fixed them.** Every
  previous attempt was tested by importing the module and faking a
  terminal, which kept passing while the real thing failed. Now there is a
  harness (`drive.py`) that forks the actual program under a real
  controlling terminal and scripts keystrokes into it. The first run of it
  immediately showed the Ctrl-C menu never accepting input — because
  `interrupt_menu` called `input()` directly while a reader thread was
  blocked on stdin and swallowing the line.
  The rule now: **exactly one thing owns stdin at a time.**
  - One reader thread, alive only during playback, for immediate keys.
  - Menus and the Ctrl-C menu use `input()` — the one path that has always
    worked in a-Shell.
  - `retire()` tells the reader to stop after its next read, and `ask()`
    waits up to a second for it to hand that keystroke over through the
    buffer, so the handover loses nothing. `input_pump()` starts a fresh
    reader if the last has retired.
  Verified against the real binary in a pty: raw `q`; pause, resume, quit;
  arrow seek then quit; Ctrl-C quit; Ctrl-C resume then quit; Ctrl-C seek
  +10 then quit. All six pass.

- [x] **49. Popular returned hashtag spam.** Jakub's run showed both
  `feed/trending` attempts failing — YouTube retired the page in July 2025
  and it now redirects to the home page, which yt-dlp reports as
  "channel/playlist does not exist" — and the search fallback then returned
  twenty livestreams whose titles contain `#trending`.
  "now" is no longer a search. It merges the real category pages
  (`/music`, `/gaming`, `/movies`), deduped, a few from each.
  `feed/trending` is tried only after those, in case a region still serves
  it. The search seeds no longer contain the word "trending", which is what
  matched the spam: they describe content instead ("official music video
  new").

- [x] **48. Input rebuilt around one blocking reader thread.** `skjyt keys`
  on a-Shell gave the answer: `/dev/tty` is `PermissionError`, stdin
  *reports* as a tty, termios/tty/fcntl/select are all present — and a
  non-blocking read returns nothing whatsoever. Its stdin is not a real
  pty. Meanwhile the toggle-per-read trick was what made stdout
  non-blocking on a desktop. Every non-blocking scheme failed somewhere.
  `InputPump` is one daemon thread doing an ordinary blocking `os.read` on
  stdin, appending to a shared buffer. `Keys.drain()` and every prompt read
  from that buffer, so nothing competes for stdin. Raw mode is now
  best-effort: with cbreak you get single keys, without it the same keys
  arrive at Enter — the program works either way instead of failing.
  Verified through a pty in both modes: single key with no Enter, key plus
  Enter in canonical mode, `ask()` taking a line from the same buffer, and
  playback quitting on `q`.

- [x] **47. Controls: stopped guessing, added a path that cannot fail.**
  Three rounds of fixing this by hypothesis, each verified only against
  whichever branch this container happens to take, and the last one broke
  a-Shell as well. The `select()` call added in round three is the likely
  mobile regression — select on stdin was unreliable in a-Shell, which is
  why it was removed in the first place. Reverted.
  - **Read from every source, in order, rather than choosing one.**
    Private `/dev/tty` handle first, then stdin with the flag toggled per
    read. Whichever returns bytes wins, and `Keys.working` records which
    one so the diagnostic can report it. Guessing cost three rounds;
    reading both costs microseconds.
  - **Ctrl-C now opens a control menu instead of ending the video.** Plain
    `input()`, no raw mode, no termios — so it works on any terminal that
    exists. Resume, seek to a timestamp or by ±N, next, quit. A second
    Ctrl-C from inside it still quits. The SIGINT handler is installed for
    the duration of playback only and restored in `finally`.
  This is the important part: control no longer *depends* on raw-mode key
  reading working. If the keys are dead, Ctrl-C still gets you a menu.
  Verified through a pty: raw `q`, Ctrl-C then quit, Ctrl-C then resume then
  raw `q`, and Ctrl-C then seek +10 (landed at 10.6s) all behave, with
  termios and the SIGINT handler restored each time.

- [x] **46. Controls dead during playback, and the menu unusable after.**
  Several causes, and the second one explains the "after" half:
  1. Keys typed during playback that we ignored stayed in the terminal's
     input queue. The next `input()` swallowed them, so the menu appeared to
     act on its own or refuse to respond. `Keys.flush()` now discards the
     queue with `tcflush` on both enable and restore.
  2. The private `/dev/tty` handle was read with a bare non-blocking
     `os.read`. Some terminals never return data that way. It now checks
     `select` first and falls through to the read.
  3. `/dev/tty` was used without confirming it is a terminal; now
     `os.isatty` gates it and it falls back to stdin if not.
  Added `skjyt keys`: prints which input path was chosen, whether
  `/dev/tty` opens, whether stdout is accidentally non-blocking, then
  echoes keypresses live. `skjyt doctor` reports the input path too.
  Verified through a pty: a burst of pause / resume / arrow / junk / quit
  all land correctly, a single arrow produces exactly one seek, termios is
  restored with ECHO and ICANON back on, and no stale keystrokes reach the
  menu.

- [x] **45. "write could not complete without blocking".** My own bug, and
  a subtle one. The rewritten key input set `O_NONBLOCK` on file descriptor
  0. That flag lives on the *open file description*, not the descriptor,
  and a shell hands the same description to stdin, stdout and stderr — so
  stdout became non-blocking too, and the first frame-sized write died
  part-way through with `BlockingIOError`.
  Fixed at the source: `Keys` opens `/dev/tty` separately where it can,
  giving a private description whose flags affect nothing else. Where that
  is unavailable it falls back to fd 0 and sets the flag only for the
  instant of the read, restoring it immediately.
  Fixed again at the sink: all playback output goes through `emit()`, which
  handles a partial write by resuming from `characters_written` rather than
  losing the rest of the frame — because a parent process can hand us a
  non-blocking stdout regardless of what we do.
  Verified on a pty: stdout stays blocking while keys are active, a 200 KB
  frame writes whole, and `emit` completes a 300 KB write against a
  deliberately non-blocking stdout where a plain `sys.stdout.write` does
  not.

- [x] **44. Audio dying killed the picture with it.** On a box with no
  sound device — a container, WSL without a sound server, a headless
  machine — `ffplay` exits the moment it starts. Since the video reads its
  frame number off the audio clock, `clock.finished()` went true straight
  away and playback stopped after three frames of a sixty-frame clip.
  Two guards: `CommandSound` now waits a third of a second after spawning
  and treats an immediate exit as a failed backend, so it falls back to
  silent playback with a clear reason. And if the audio dies *mid-way*,
  `Clock.disown()` hands the clock over to wall time at the current
  position and the picture carries on, with a one-line notice. 60 frames
  where there were 3.

- [x] **43. Two extractions, twice the failure surface.** `fetch` asked
  yt-dlp for video, then asked again for audio — so the fallback ladder ran
  twice, took twice as long, and had two chances to hit YouTube's
  intermittent errors. That is exactly how it failed in practice: the video
  came back fine on the third rung and the audio pass then died on all
  seven.
  Now one extraction, preferring a pre-muxed format so there is nothing to
  merge, and the single file is handed to both the frame decoder and the
  audio player — which is how a local file already worked. `has_audio()`
  checks with ffprobe before handing a silent file to the player. Subtitles
  get one attempt and no ladder, since they are optional and already
  non-fatal.

- [x] **42. The retry list was the wrong shape.** First attempt at the
  ladder used an allowlist of errors worth retrying. "The page needs to be
  reloaded" matched nothing in it, so the ladder quit three rungs early on
  a video that was fine. Inverted: everything is retried except a short
  `FINAL` list of errors that are genuinely the truth (private,
  members-only, removed, age-restricted, geo-blocked). Enumerating
  YouTube's failure messages is a losing game; enumerating the handful that
  are final is not.
  Seven rungs now, ending with a plain repeat of the defaults, with a short
  backoff between them — this particular error is intermittent, with
  reports of roughly one success in ten on an unchanged command.
  Also: yt-dlp colours its own ERROR prefix, and those escape codes were
  being printed literally in our message. Stripped, along with the
  `[youtube] <id>:` tag, so the reason reads as a sentence.

- [x] **41. "This video is not available" on videos that play fine.**
  Diagnosed wrong the first time: this is not the video being gone, it is
  `raise_no_formats` — yt-dlp found formats, decided every one of them
  needed a PO token it does not have, skipped them all, and reported the
  video as unavailable.
  The old code retried only on JS-signature errors, so it never tried
  anything here. There is now a five-rung ladder: yt-dlp's own defaults,
  then `formats=missing_pot`, then the tv/web_safari/android_vr players,
  then ios/android/web, then the same with the quality limit dropped.
  Deliberately starts with yt-dlp's defaults and only then overrides the
  player client — a hardcoded client list rots fast, since which clients
  still serve free formats changes every few months.
  Genuinely-gone videos (private, members-only, removed, age-restricted)
  match a `FINAL` list and raise on the first attempt rather than grinding
  through five pointless retries.
  Also: `skjyt update` runs the pip upgrade, and `skjyt doctor` flags a
  yt-dlp build more than 60 days old, since an outdated yt-dlp is the single
  most common cause of this.

- [x] **40. One dead video killed the whole session.** An unavailable item
  in a queue let `yt-dlp`'s `DownloadError` propagate all the way out of
  `main()`. Now `run_one` catches it and returns `skip`, the queue loop
  reports and advances, and a broad handler around `run_one` catches
  anything else unexpected so a single bad item can never end a session.
  One-shot runs exit non-zero instead of tracebacking.
  Two supporting changes: yt-dlp gets a `logger` so it stops printing its
  own ERROR block into the middle of a drawn screen, and `describe_error()`
  turns its message into something actionable — private, members-only,
  age-restricted, removed, geo-blocked, premiere not started, JS challenge,
  TLS, network — falling back to the first line otherwise. The match needles
  are deliberately specific: an early draft matched on `"age"`, which also
  matches "page", "message" and "storage".

- [x] **39. Settings screen crashed on the new modes.** `MODE_BLURB` was
  never given entries for `braille` or `quad`, so opening settings with
  either selected raised `KeyError` and killed the program. Descriptions
  added, the lookup goes through `mode_blurb()` which falls back to the
  pixel count rather than raising, and `skjyt doctor` now checks that every
  mode in `MODES` appears in every table indexed by mode — so the next mode
  added gets caught by a check instead of by a traceback.

- [x] **38. Sparse seeking on a-Shell.** Slices used to be appended into one
  file, so jumping near the end of a long video meant decoding everything
  before it. Each slice is now its own file keyed by start time, with a
  `slice_frames` index, and `locate()`/`want()` decode exactly the one slice
  a seek lands in. Measured on a 30s clip with 5s slices: seeking to 27s
  decoded slice 5 only, in 0.20s, holding slices `[0, 5]` and nothing
  between. `FrameReader` hides whether frames live in one file or several,
  so the streaming path is unchanged. `available()` still reports the
  contiguous run from zero, which is what the buffered bar means.
  Also fixed a hang this exposed: at the end of a sparsely-decoded video the
  loop asked for a slice past the end forever. It now stops.

- [x] **37. Cache validated, not trusted.** A filename is a promise, not
  evidence — a truncated file has the same name as a good one. Each cached
  frame file now has a `.json` sidecar recording byte length, frame size,
  frame count and the source id plus duration; lookup re-checks all of them.
  Verified it rejects a wrong source, a wrong frame size, a truncated file
  and a missing sidecar.

- [x] **36. Feed negative signal.** `-` in the feed hides the *reason* a row
  appeared, not just that video: a keyword row suppresses the keyword, a
  channel row suppresses the channel. Stored in `state.json`, applied in
  `build_feed` at both the source and the result stage, and it drops the
  cached feed so the change shows immediately. `skjyt muted` lists what is
  hidden, `skjyt muted clear` undoes it.

- [x] **35. Channel search.** `/` on a channel page searches within that
  channel via its public `search?query=` page — no sign-in, and it makes big
  back catalogues usable. Also `skjyt channel <@handle> search <words>`.

- [x] **34. Thumbnails fetched in parallel.** A cold page of eight rows was
  eight round trips. Downloads now go through a small thread pool before the
  page draws. Only the downloads are parallel: decoding goes through ffmpeg,
  which on a-Shell runs in-process via `os.system` and is not safe to call
  from several threads. Falls back to a plain loop if threads are
  unavailable.

- [x] **32. Braille and quadrant modes.** The old modes wasted the cell: a
  character can carry more than one pixel. `quad` uses the 2×2 quadrant
  glyphs for 4 pixels a cell, taking the brightest pixel as foreground and
  the darkest as background — picking extremes rather than averaging the
  two groups looks identical at this size and took the cost from 10.5 ms to
  3.3 ms. `braille` uses U+2800–U+28FF for 8 pixels a cell, monochrome, and
  turns out to be both the sharpest mode and nearly the cheapest: 415
  B/frame on a real clip against ascii's 630, with eight times the pixels.
  Geometry is generalised through a `SUBPIXELS` table, so `pixel_size`,
  `rendered_rows` and the aspect correction all derive from one place
  instead of special-casing `blocks`.

- [x] **31. 256-colour mode.** `\x1b[38;5;Nm` against the 24-bit form, with
  a 6×6×6 cube plus the 24-step grey ramp. Measured **38% fewer bytes** in
  every colour mode. Default in three of four presets.

- [x] **30. Colour escapes memoised.** Quantisation leaves only a few dozen
  distinct colours per frame, so a dict cache hits nearly every cell and the
  renderer stops formatting strings in the hot loop.

- [x] **Image quality knobs.** `unsharp` before the downscale (settings `u`),
  `eq=contrast` (settings `v`), and the scaler moved from `bilinear` to
  `lanczos` (settings `z`). All in ffmpeg, so they cost nothing in Python.
  Sharpening before a downscale to a few thousand pixels does more for
  perceived quality than anything in the renderer.

- [x] **29. Media controls (partial).** `NowPlaying` publishes title, artist,
  duration, elapsed and rate to `MPNowPlayingInfoCenter`, so the lock screen
  and Control Center show what's playing. The property names are read as
  real symbols out of the MediaPlayer framework with `in_dll` rather than
  guessed as string literals. `pump_runloop()` gives CoreFoundation a
  zero-timeout turn each frame so the system UI stays current. Cleared on
  exit. Acting on the lock-screen buttons is deliberately not done — see
  Still open.

- [x] **28. Controls missing keypresses.** Four separate causes, all fixed:
  1. `select()` on stdin isn't dependable in a-Shell, where the terminal
     isn't a real pty. Replaced with `O_NONBLOCK` + `os.read()`.
  2. Only one key was consumed per frame, so a burst queued behind the
     render loop and felt dead. `drain()` takes everything pending.
  3. Arrow keys send escape sequences — Right arrived as `\x1b`, `[`, `C`,
     and `[` is the subtitle toggle, so arrows toggled subtitles. Sequences
     are parsed now (CSI and SS3, parameterised forms too), and unknown
     ones are dropped rather than guessed, so Delete (`\x1b[3~`) does
     nothing instead of something wrong.
  4. Running ffmpeg via `os.system()` on a-Shell hands the terminal to a
     child, leaving cbreak off afterwards. `reapply()` restores it after
     every external command.
  Also: keys are lowercased so `Q` works, arrows seek 5s and 30s, `k`/`j`/`l`
  added, `r` forces a repaint, and Escape is deliberately *not* bound to
  quit — a sequence split across two reads would look exactly like it.
  Verified against a real pty, including the reapply path.

- [x] **27. Thumbnails.** A small picture per row in search, feed, popular
  and channel lists, drawn with the same renderer as the video —
  half-blocks, or plain characters under the performance preset. URLs come
  from yt-dlp's own thumbnail list where present, else the standard
  `i.ytimg.com/vi/<id>/mqdefault.jpg`; fetched with stdlib `urllib`, no new
  dependency. Decoded pixels are cached alongside the frame cache, so a
  repeat list draws in ~0.15 ms. Rows are taller, so lists now page with
  `<` and `>` — 3 per page on a 24-row terminal against 7 without.
  Network failures degrade to blank space rather than breaking the list.
  Toggle in settings (`p`) or `--no-thumbs`; the performance preset turns
  them off by itself.

- [x] **26. Setup screen.** Runs automatically when there is no config file,
  or on `skjyt setup`. Checks python, yt-dlp, ffmpeg, an audio backend,
  whether the config dir is writable, terminal size, truecolour and free
  disk, marking each pass or fail. Offers to `pip install` what's missing
  (`--no-deps` on a-Shell, since only pure-Python wheels work there). Then
  asks performance / balanced / quality and applies a preset. `skjyt doctor`
  is the same check for the command line, with a non-zero exit if anything
  fails, and it states the dependency list outright: yt-dlp is the only
  library, ffmpeg and ffplay the only binaries, stdlib otherwise.

- [x] **25. Auto-tune.** `skjyt tune`, and part of setup. Measures the two
  real ceilings — Python's ms/frame and the terminal's bytes/frame — and
  sets the cell budget from whichever binds, then clamps to the screen. It
  reports *which* one bound, because that's the actionable part: render
  speed means drop fps or mode, throughput means cheaper mode or a bigger
  colour step, screen size means you have headroom to shrink the font.
  Verified across all four modes and both platform paths; blocks on a phone
  lands at ~814 cells, ascii at the screen limit.

- [x] **24. Channels page.** `channel_url()` takes a full URL, an `@handle`
  or a bare name and normalises it, stripping any tab suffix. Four tabs —
  videos, shorts, streams, playlists — switchable in place. `f` follows or
  unfollows. Following is a local list in `state.json`, no account involved,
  and followed channels feed the recommender at the highest weight above
  everything else.

- [x] **23. Popular.** YouTube retired the single global Trending page in
  July 2025, so nothing trusts one URL. Each category — now, music, gaming,
  movies — has a candidate list, tried in order, with a plain search as the
  last resort; the screen shows which source actually answered. Region is a
  setting (`gl=`), default PL, changeable inline with `c`.

- [x] **22. A real CLI.** Subcommands rather than menu-only: `feed`,
  `popular [category]`, `channel <@handle> [tab]`, `follow`, `unfollow`,
  `following`, `history [n]`, `cache [info|clear]`,
  `config [path|get|set k v]`, `bench`, `replay`, `commands`. `config set`
  type-checks against the current value and rejects unknown keys.
  BrokenPipeError is swallowed so `skjyt config get | head` behaves.

- [x] **21. A feed.** YouTube's own recommendations need account cookies,
  which a phone can't supply, so the recommender is local. Every watch over
  15s is logged with title, channel and how much of it you got through.
  `taste()` turns that into weighted channels and keywords, with a 30-day
  recency half-life and completion as a quality signal — a video you
  abandoned after 6 seconds counts for almost nothing. The feed then mixes
  more-from-that-channel (highest weight), searches for words that recur in
  titles, and trending as filler. Deduped, already-watched excluded, each
  row labelled with why it's there. Cached 6 hours, `r` to refresh,
  `--feed` to jump straight in. Cold start falls back to pure trending.

- [x] **1. Streaming / chunked decode.** `Decoder` no longer makes you wait
  for the whole file. On PC ffmpeg runs as a child writing the frame file
  while playback reads it as it grows. On a-Shell, where there is no
  fork/exec, it decodes a slice at a time (`chunk` setting, 15s default) and
  appends. Playback starts after a 2-second lead. When the playhead reaches
  the end of what exists, it shows "buffering …" and — on the a-Shell path
  only — pauses the audio while the next slice decodes, so sound can't run
  away from the picture. The progress bar shows buffered extent in green.

- [x] **2. Disk-space check.** `Decoder.check_space()` projects
  `frames × w × h × channels` and compares against `shutil.disk_usage()`.
  Over 80% of free space, it warns with both numbers and asks.

- [x] **3. Resize handling.** `SIGWINCH` sets a flag; `play()` returns
  `"resize"` with the current position, and `run_one` re-grids, re-decodes
  and resumes at that spot. The media is fetched once and reused across
  resizes, so it doesn't re-download.

- [x] **4. `Painter` periodic full repaint.** Every 100 frames it ignores its
  own diff state and rewrites everything, so anything else that wrote to the
  screen gets cleaned up. Also `\x1b[K` per line now, so shorter lines don't
  leave tails.

- [x] **5. Seek clamp.** `Clock.goto()` is the single clamp point and both
  audio and video derive from it, so they can't land on different frames
  near zero. `nudge()` routes through `goto()`.

- [x] **6. Terminal restore.** `Keys.restore()` is callable directly rather
  than only via `__exit__`, `play()` calls it in its `finally`, `die()` and
  `main()` both restore, and `main()` has a top-level `finally`.

- [x] **7. yt-dlp JS challenge fallback.** `_extract()` wraps every call: on
  an error mentioning player/nsig/signature/challenge it retries with
  `player_client: [android, web]`. The search screen also names the problem
  when it sees one.

- [x] **8. Frame cache.** `~/.skjyt/cache/<id>-<w>x<h>-<fps>-<mode>-…​.raw`
  with a `.ok` marker so half-written files are never reused. LRU trim to a
  configurable limit (1 GB default), size shown in settings, `clear` to
  empty. Replaying Bad Apple is now instant.

- [x] **10. One renderer.** `render_lines()` replaced three near-copies.
  `ascii` keeps its `bytes.translate` fast path, `blocks` pairs rows,
  `squares` and `color` differ only in glyph choice.

- [x] **11. Quantisation moved into ffmpeg.** `lutrgb` with a `trunc`
  expression does it in the filter chain; Python no longer touches every
  channel. `filter_chain()` is the single place that builds the chain.

- [x] **12. Cell aspect is a setting.** Default 2.0, adjustable 1.0–3.0,
  since real cells are usually nearer 2.1:1 and it varies by font.

- [x] **13. Playlists and queue.** yt-dlp flat playlist listing, "queue all"
  on both playlists and search results, a queue screen, and `n`/`p` to move
  through it during playback.

- [x] **14. Local files.** A path that exists skips the network entirely.
  `ffprobe` supplies the duration. Also the easy way to test render changes
  offline.

- [x] **15. Resume position.** Per-video-id in `state.json`, prompted on
  open, cleared when a video finishes within 15s of the end.

- [x] **16. Subtitles.** yt-dlp fetches VTT (including auto-generated),
  a small parser strips karaoke tags, two centred lines render under the
  video, `[` toggles them live.

- [x] **17. Dithering.** Done in ffmpeg rather than Python: `format=monob`
  makes swscale dither to 1-bit, then back to gray. Free, and it makes
  gradients survive ascii mode.

- [x] **18. Benchmark.** `--bench` renders 24 synthetic frames in all four
  modes and prints cells, ms/frame, and bytes/frame both full and diffed,
  plus the byte budget for your fps. Tune against numbers, not feel.

- [x] **19. Record and replay.** `--record out.cast` writes timestamped
  escape payloads; `--replay out.cast` plays them back at the original
  timing.

- [x] **20. Settings not saving on iOS.** a-Shell's home is the app
  container and is read-only — only `~/Documents` is. Now probes candidate
  directories, migrates a legacy `~/.skjyt.json`, reads back what it wrote,
  and shows the path plus the real exception on failure.

- [x] **PC support.** Capability probe over platform guessing; Apple and
  ffplay backends; Windows VT, UTF-8 and msvcrt; real argv for ffmpeg;
  desktop cell budget defaults to 12000.

---

## Partial

- [~] **ffplay start latency.** Not measured automatically, but there is now
  an `audio delay` setting (±2s) to compensate once you know your number.
  Auto-calibration would need a loopback recording; not worth it.

- [~] **Windows.** Written to the documented API and structurally exercised,
  but not run on real Windows. Treat as untested.

---

## Not doing

- [-] **Palette rendering.** Superseded, and measured rather than assumed.
  On a real clip in `blocks` mode: current default (256-colour output plus
  `lutrgb` quantisation at step 24) gives 11635 B/frame. Routing through a
  fixed palette with `format=rgb8` gives 14883 with 256-colour output and
  22732 with truecolour. The palette is *worse*, because quantisation
  already collapses the colour count and the 256-colour escapes already
  shorten the codes. `palettegen`/`paletteuse` would also need a second
  pass over the whole video. No reason to do it.

- [-] **Lock-screen buttons.** Needs an Objective-C block for
  `MPRemoteCommandCenter`. Constructible in principle
  (`_NSConcreteGlobalBlock` literal), but a malformed one crashes the app
  rather than raising, it cannot be tested from here, and it would need the
  run loop serviced far more often than once a frame. Display-only stands.

- [-] **9. Split the file into a package.** Argued against: the whole point
  is that one file can be dropped into a-Shell's Documents folder and run.
  A package means moving a directory onto iOS, which is materially worse for
  the main platform. The file is sectioned with banner comments instead. If
  it passes ~2000 lines, revisit.

---

## Still open

- [ ] **mpv backend** with `--input-ipc-server` for gapless seeking on PC.
  ffplay has to respawn with `-ss` to seek, which is audible. mpv over a
  socket would fix it, at the cost of a second binary that most people
  won't have. Worth doing only if the seek gap actually annoys someone.
