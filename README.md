# zoho-tui

Terminal client for Zoho Mail, built on the Zoho Mail REST API and Textual.

## Install

```sh
uv venv .venv && uv pip install -p .venv -r requirements.txt
ln -s "$PWD/zoho" ~/.local/bin/zoho
mkdir -p ~/.config/zoho-tui
cp config.env.example ~/.config/zoho-tui/config.env && chmod 600 ~/.config/zoho-tui/config.env
```

Fill in `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET` and `ZOHO_REFRESH_TOKEN` (a Zoho self-client with the `ZohoMail.messages.ALL`, `ZohoMail.folders.ALL` and `ZohoMail.accounts.READ` scopes). Account ID and from-address are looked up automatically. Any `ZOHO_*` environment variable overrides the file.

## Keys

| Key | Action |
| --- | --- |
| `j` / `k`, arrows | Move |
| `tab` | Switch between folders, list and reader |
| `enter` | Open folder |
| `c` | Compose |
| `r` / `R` | Reply / reply all |
| `a` | Archive (moves to the Archive folder) |
| `d` | Delete (moves to Trash) |
| `u` | Toggle read |
| `f` | Toggle flag |
| `/` | Search (`invoice`, `from:bob`, `subject:hi`) |
| `g` | Refresh (also runs every 60s) |
| `q` | Quit |

Opening a message marks it read. In the compose window, `ctrl+s` sends and `esc` cancels. Messages are sent as plain text. Attachments are flagged but not downloadable yet.
