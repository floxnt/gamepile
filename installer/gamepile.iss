; ============================================================================
; GamePile — Inno Setup installer script
; ============================================================================
;
; Per-user Windows installer for the PyInstaller --onedir bundle produced by
; gamepile.spec. Replaces the raw .zip artifact the project shipped through
; v0.7.0 with a proper installer-and-uninstaller package starting v0.8.0.
;
; Build (from the repo root, with dist\gamepile\ already populated by
; PyInstaller):
;
;   ISCC.exe /DAppVersion=0.8.0 installer\gamepile.iss
;
; Output: gamepile-setup-v0.8.0.exe in the repo root (matches OutputDir=..).
;
; Architecture decisions (locked, not re-litigated here — see
; SPEC_V5_DISTRIBUTION.md "Inno Setup installer" section for full rationale):
;
;   - Per-user install (PrivilegesRequired=lowest). No admin elevation, no
;     UAC prompt. Matters for the SmartScreen-shy friend audience: the
;     unsigned-binary "More info → Run anyway" click is already the friction
;     gate; we don't add a second UAC dialog on top.
;
;   - Installs to {userpf}\GamePile, which resolves to
;     %LocalAppData%\Programs\GamePile under PrivilegesRequired=lowest. Per-
;     user Programs is the standard lowest-privilege install location since
;     Inno Setup 6.
;
;   - DOES NOT TOUCH %LocalAppData%\gamepile (user data — the gamepile.db
;     SQLite file lives there). The installer scope is the program files
;     only; the uninstaller scope is the same. User data survives uninstall
;     and reinstall by virtue of platformdirs separation between the install
;     location and the user-data location. If a user wants to fully remove
;     their data after uninstall, README.bundled.md documents the one-line
;     manual cleanup.
;
;   - Stable AppId GUID across all versions. Inno detects an existing
;     install by AppId and offers in-place upgrade; regenerating the GUID
;     per release would orphan prior installs and prevent the upgrade path
;     from working. Do NOT regenerate this on a future bump.
;
;   - Publisher = "floxnt" (matches the GitHub repo owner / project
;     pseudonym used throughout the codebase). The Publisher column in
;     Add/Remove Programs identifies the maker, not the app.
;
;   - Unsigned. Code signing is deferred — see SPEC_V5_DISTRIBUTION.md
;     "No code signing" section. SmartScreen "More info → Run anyway" is
;     the expected first-launch experience.
;
; Version sourced from the build via /DAppVersion=X.Y.Z on the ISCC command
; line. Never hardcode the version in this file.

#ifndef AppVersion
  #error AppVersion must be passed in via /DAppVersion=X.Y.Z on the ISCC command line.
#endif

#define MyAppName        "GamePile"
#define MyAppPublisher   "floxnt"
#define MyAppURL         "https://github.com/floxnt/gamepile"
#define MyAppExeName     "gamepile.exe"
#define MyPayloadDir     "..\dist\gamepile"
#define MyOutputDir      ".."

[Setup]
; Stable AppId — fixed GUID, do NOT regenerate. Inno uses this to detect an
; existing install for upgrade-in-place behavior. Generated 2026-05-19 via
; uuid.uuid4(); committed as the canonical identity for the installed app
; across every future release.
AppId={{D72A3C2F-1F81-4B71-80C5-AFF7276673BD}
AppName={#MyAppName}
AppVersion={#AppVersion}
VersionInfoVersion={#AppVersion}
AppVerName={#MyAppName} {#AppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases

; Per-user install. lowest = no admin elevation, no UAC. {userpf} resolves
; to %LocalAppData%\Programs under lowest-privilege.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={userpf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
AllowNoIcons=yes

; Output. Installer lands one level up from this .iss file (repo root), so
; the workflow's Publish Release step can attach it without path drama.
OutputDir={#MyOutputDir}
OutputBaseFilename=gamepile-setup-v{#AppVersion}

; Add/Remove Programs entry — display name, icon, publisher all match the
; in-app identity. Size-shown is set automatically by Inno from the actual
; install payload.
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}

; Compression. lzma2/ultra is the Inno standard for "small installer at the
; cost of compile time"; the CI runner can spare the seconds.
Compression=lzma2/ultra
SolidCompression=yes

; If GamePile is running when the user double-clicks the installer (the
; v0.7.0 → v0.8.0 upgrade case), Inno offers to close it cleanly rather
; than failing the file replace. RestartApplications=no prevents the
; auto-restart of the closed instance — the [Run] section's "Launch
; GamePile" postinstall task is the one canonical relaunch path.
CloseApplications=yes
RestartApplications=no
CloseApplicationsFilter=*.exe,*.dll,*.pyd

; Custom app icon. Used in the installer wizard and as the installed
; application's icon (Start Menu, desktop shortcut, Add/Remove Programs).
SetupIconFile=..\assets\icons\gamepile-icon.ico

; Modern visual style. No custom wizard pages; the defaults are right for
; a friend-distribution installer.
WizardStyle=modern
ShowLanguageDialog=no

; Architecture — 64-bit only. The CI runner builds x86_64; running this
; installer on a 32-bit Windows is unsupported regardless.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
; Optional desktop shortcut. Checked by default — friends expect a desktop
; icon from a Windows installer. They can uncheck it during install if they
; don't want one.
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; The entire PyInstaller --onedir payload. recursesubdirs + createallsubdirs
; walks the full tree. ignoreversion forces the installer to always copy
; (avoids "the file at the destination is newer" prompts on downgrade
; scenarios; matches the simple replace-all upgrade semantics).
Source: "{#MyPayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; App icon for shortcuts — installed alongside the payload so Start Menu
; and desktop shortcuts reference it via {app}\gamepile-icon.ico.
Source: "..\assets\icons\gamepile-icon.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; Start Menu shortcut — primary launch path for friends.
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\gamepile-icon.ico"
; Uninstall shortcut alongside, per the standard Inno pattern.
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
; Optional desktop icon — gated by the [Tasks] entry above.
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\gamepile-icon.ico"; Tasks: desktopicon

[Run]
; Postinstall: offer to launch GamePile when the installer finishes. Checked
; by default — friend audience expects "Finish" to do something. nowait so
; the installer process exits cleanly even if GamePile takes a moment to
; render its window. postinstall + skipifsilent confine this to interactive
; runs (no auto-launch during silent /SILENT installs).
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

; ============================================================================
; What this installer DOES NOT do (deliberate, do not "fix"):
;
;   - No [UninstallDelete] entry for %LocalAppData%\gamepile. User data
;     survives uninstall, full stop. Months of accumulated Steam sync,
;     manual classifications, pick history, and affinity weights are
;     valuable and irrecoverable if deleted; a misclick "also remove user
;     data" checkbox is exactly the destructive UI we will not add. See
;     SPEC_V5_DISTRIBUTION.md "User data preservation across uninstall"
;     for the cost-asymmetry rationale and the documented manual cleanup
;     path in README.bundled.md.
;
;   - No code signing. Unsigned installers and unsigned executables ship
;     as the friend-distribution baseline. SmartScreen "More info → Run
;     anyway" is the expected first-launch experience and is documented
;     in README.bundled.md. Revisit if/when friend-count grows.
;
;   - No automatic update mechanism. Friends re-download a new installer
;     from the Releases page when a new version drops; the stable AppId
;     means a fresh install-over-old-install upgrades cleanly without
;     touching user data. Built-in auto-update (Sparkle/Squirrel style)
;     is rejected scope as of v0.8.0.
; ============================================================================
