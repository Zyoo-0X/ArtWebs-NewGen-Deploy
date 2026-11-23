# ArtWebs Games Deployer

A small Python tool. It extracts Artemis Engine PFS archives on your PC and uploads the game files to a GitHub repository.

## Layout on GitHub

```text
games/
└── GameName/
    ├── system.ini
    ├── system/
    ├── image/
    ├── script/
    ├── sound/
    └── ...
```

- PFS archives are extracted first. The raw `.pfs` files are not uploaded.
- Loose files (`.ini`, `.exe`, `.lua`, ...) are copied as they are.
- Your folders inside `games/` are never changed or deleted.
- A file bigger than the max file size (default 90 MB) is split into `.part000.bin` pieces and a `manifest.json` is created for it.

## Requirements

- Python 3.10 or newer
- Git for Windows
- A GitHub repository and a Personal Access Token with **Contents: Read and write**

## Quick start

1. Put each game inside `games/`:
   - Folder: `games/MyGame/root.pfs`, `games/MyGame/root.pfs.000`, and any loose files.
   - Or a loose archive: `games/MyGame.pfs`, `games/MyGame.pfs.000`.
2. Run the script:

   ```text
   python deploy_games.py
   ```

3. Open **5. Settings**, then set **1. GitHub URL** and **5. GitHub token**.
4. Open **1. Upload games**, choose a game, and confirm.

The first time a PFS archive is found, the tool downloads `pfs-rs` (the extractor) and saves it in `tools/`. You can also put `pfs-rs.exe` in `tools/` yourself.

## Menu

```text
1. Upload games
2. Update a game
3. View games
4. Delete a game
5. Settings
0. Exit
```

- **Upload games**: extracts the selected games and uploads them. You can type `1`, `1,3` or `A` for all.
- **Update a game**: picks a game that is already on GitHub, compares it with your local copy in `games/`, and uploads only the files that are missing or different. Nothing on GitHub is deleted. If the game is not in `games/`, it asks for the path of a folder with the patch files.
- **View games**: opens the selected game folder on the GitHub website.
- **Delete a game**: removes `games/<name>` from GitHub after you type `DELETE`. Local files are kept.
- **Settings**: pick a number to change it.

## Settings

| No. | Setting | Default |
| --- | --- | --- |
| 1 | GitHub URL | `https://github.com/` (set your own) |
| 2 | Branch | `main` |
| 3 | Max file (MB) | 90 |
| 4 | Batch push (MB) | 250 |
| 5 | GitHub token | (empty) |
| 6 | Debug mode | OFF |
| 7 | Debug log | |

Settings are saved in `.games.json`. The token is saved in `.env`.

## Upload and resume

- Files are pushed in batches (about 250 MB each).
- If GitHub rejects a push (HTTP 500), the batch is split in two and tried again, until it goes through.
- If the upload stops (error, closed window), start the script again. It offers to continue and skips files that are already on GitHub.
- A list of every pushed file is written to `logs/pushed_<time>.txt`.

## Debug

- Turn on **Debug mode** in Settings, or start with `python deploy_games.py --debug`.
- Everything is always written to `logs/games.log`. Use **Settings > Debug log** to read the last lines.

## Other commands

```text
python deploy_games.py --scan                 list the games in games/
python deploy_games.py --prepare "GameName"   extract one game into build/ only
```

## Folders and files

| Path | Purpose |
| --- | --- |
| `games/` | Your local games (not uploaded as they are) |
| `build/` | Extracted files, made before upload |
| `.github/` | Small local Git folder used for pushing |
| `tools/` | The `pfs-rs` extractor |
| `logs/` | Log files |
| `.games.json` | Saved settings |
| `.env` | Your GitHub token |
| `games.save` | Resume checkpoint |

Do not upload `.env`, `.games.json` or `logs/` anywhere public. They contain your token and settings.

## Note

This tool only stores the game files on GitHub. It is not a browser player. To play the games on a website, the site needs the Artemis web runtime and code that loads `games/<name>/`.

## Credits

Extraction uses [pfs-rs](https://github.com/sakarie9/pfs-rs) (MIT license).