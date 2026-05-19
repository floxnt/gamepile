# GamePile

GamePile is a desktop app that helps you make progress through your Steam
backlog. It pulls your library, enriches it with playtime estimates
(HowLongToBeat), engagement signals (Steam achievements + reviews), and
gives you a weighted recommendation engine that learns your taste over time.

The headline feature is **Shortlist** — pick a mode ("I only have tonight",
"Continue something", "Comfort pick", "Start something new", "Surprise me")
and GamePile suggests 5 games tailored to your library, time window, and
past picks. The broader purpose is backlog management: one Library view
of everything you own, a Backlog view of the unplayed pile, and per-game
detail pages with personal notes and ratings.

Local-only. Single-user. No cloud sync. No telemetry.

---

## Installing

### Windows

1. Download `gamepile-setup-vX.Y.Z.exe` from the
   [Releases page](https://github.com/floxnt/gamepile/releases).
2. Double-click the installer. It's a per-user install — no admin
   password required.
3. Click through the wizard. The defaults are fine. Optionally tick
   "Create desktop icon" if you want one.
4. When install finishes, GamePile launches automatically (the
   "Launch GamePile" checkbox on the Finish page).
5. Subsequent launches: Start Menu → GamePile, or the desktop icon if
   you created one.

**SmartScreen warning:** Windows will show "Microsoft Defender SmartScreen
prevented an unrecognized app from starting" when you run the installer.
Click **More info**, then **Run anyway**. The installer is unsigned because
this is a friend-distributed build — not a sign of malware. Source code is
at <https://github.com/floxnt/gamepile> if you want to verify.

**Upgrading:** download the new installer and run it. It detects your
existing install and replaces the program files in place. Your library,
sync data, ratings, and pick history are preserved (they live separately
from the installed program files — see "Where data lives" below).

**Uninstalling:** Settings → Apps → Installed apps → GamePile → Uninstall.
Or right-click GamePile in the Start Menu and choose Uninstall. The
uninstaller removes the program files only; your data is left alone.

**WebView2 Runtime:** If your Windows 11 install is the LTSC edition (or an
older Windows 10 build), GamePile will detect a missing WebView2 Runtime
on first launch and auto-open the installer page in your browser. Install
the **Evergreen Standalone Installer** from
<https://developer.microsoft.com/en-us/microsoft-edge/webview2/>, then
re-launch GamePile. Most Windows 11 standard SKUs have WebView2 preinstalled
and won't see this prompt.

### Linux

1. Download `gamepile-vX.Y.Z-linux-x64.tar.gz` from the Releases page.
2. Extract: `tar -xzf gamepile-vX.Y.Z-linux-x64.tar.gz`
3. Install the runtime dependencies (GTK + WebKit2):

   | Distro | Command |
   |---|---|
   | Arch / Manjaro / CachyOS | `sudo pacman -S webkit2gtk-4.1 python-gobject gtk3` |
   | Ubuntu / Debian | `sudo apt install libwebkit2gtk-4.1-0 python3-gi gir1.2-webkit2-4.1` |
   | Fedora | `sudo dnf install webkit2gtk4.1 python3-gobject gtk3` |

4. Run: `./gamepile/gamepile`

These system libraries can't be bundled portably — GTK/WebKit are
distribution-specific.

## First launch

GamePile opens to a setup wizard. You'll need:

1. **A Steam Web API key.** Get one (free, instant) at
   <https://steamcommunity.com/dev/apikey>. The "Domain Name" field can be
   anything — `localhost` is fine.
2. **Your SteamID.** GamePile accepts:
   - A 17-digit SteamID64 (e.g. `76561198000000000`)
   - A vanity name (e.g. `gabelogannewell`)
   - A full vanity URL (`steamcommunity.com/id/gabelogannewell`)
   - A full profile URL (`steamcommunity.com/profiles/76561198000000000`)

The wizard validates your credentials against Steam (one test API call)
before saving them. If anything's wrong, it'll surface a useful error
and let you fix it without re-entering both fields.

Credentials are stored in your OS keychain (Windows Credential Manager /
macOS Keychain / Linux Secret Service). They never leave your machine.

After credentials are saved, GamePile kicks off the initial library sync:

- Pulls your full Steam library
- Looks up playtime estimates for each game on HowLongToBeat
- Fetches engagement signals (Steam achievements, review playtime)
- Cross-references with SteamSpy for genres + user tags

Initial sync takes ~10 minutes for a 500-game library. You can leave
the window open and come back. When sync completes, GamePile redirects
to the Shortlist tab and you're ready.

## Settings

Click **Settings** in the top-right header to edit your API key or
SteamID later. The keychain is the source of truth.

## Where data lives

| Platform | Path |
|---|---|
| Windows | `%LOCALAPPDATA%\gamepile\` (typically `C:\Users\<you>\AppData\Local\gamepile\`) |
| Linux | `~/.local/share/gamepile/` |

The `gamepile.db` file in that directory is the entire app database —
back it up if you want to preserve your pick history and ratings across
machines. The directory is also where logs land if anything crashes.

To fully remove GamePile including data: uninstall normally, then delete
`%LOCALAPPDATA%\gamepile` (Windows) or `~/.local/share/gamepile` (Linux).

## Troubleshooting

**"The app won't start" (Windows):** Make sure WebView2 Runtime is installed
(see the install section above). If GamePile flashed a console and disappeared,
that's likely the runtime check exiting because the install page got opened.

**"The app won't start" (Linux):** Check the GTK/WebKit packages are installed.
Run `./gamepile/gamepile` from a terminal — any import error surfaces there.

**"Refresh Library" times out:** Steam's API can be flaky. The refresh
resumes from where it left off — just hit the button again. HLTB lookups
have a 30-day cache, so re-running doesn't burn the rate limit.

**SteamSpy data missing for some games:** SteamSpy's free tier returns
zero for some playtime fields. GamePile flags games with "Insufficient data"
when this hits — it's a data-source limitation, not a bug.

**Some pre-2012 games show "Insufficient data" for stickiness:** Steam's
review API paginates poorly for old catalog entries; about 5% of typical
libraries fall into this. Same root cause as above — known Steam limitation.

## Filing bugs

Bug reports welcome at <https://github.com/floxnt/gamepile/issues>.
Include:
- GamePile version (visible at the bottom of the Settings page)
- Your OS + version
- What you did, what happened, what you expected to happen
- The contents of `gamepile.db-journal` if the app crashed mid-write
  (don't share `gamepile.db` itself — contains your full library)
