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

Everything is on the keyboard. Press `?` in the app for this list.

| Key | Action |
| --- | --- |
| `j` / `k` | Down / up |
| `ctrl+d` / `ctrl+u` | Half page down / up |
| `gg` / `G` | Top / bottom |
| `n` / `N` | Next / previous unread |
| `h` / `l`, `tab` | Switch pane (folders, list, message) |
| `enter` | Open folder, or read the message full screen |
| `esc` / `q` | Leave full screen or search results |
| `space` / `b` | Scroll the message from the list |
| `gi` `gs` `gd` `ga` `gt` `gj` | Inbox, Sent, Drafts, Archive, Trash, Spam |
| `gf` | Folder picker (type to filter) |
| `/` | Search (`invoice`, `from:bob`, `subject:hi`) |
| `c` | Compose |
| `r` / `R` | Reply / reply all |
| `F` | Forward |
| `a` | Archive |
| `d` | Delete (to Trash) |
| `z` | Undo the last archive or delete |
| `u` | Toggle read |
| `s` | Toggle star |
| `o` | Pick a link from the message and open it |
| `y` | Copy the sender's address |
| `ctrl+r` | Refresh (also runs every 60s) |
| `Q` | Quit |

In compose: `tab` moves between fields, `ctrl+e` edits the body in `$EDITOR`, `ctrl+s` sends, `esc` cancels (twice if you've typed something).

Opening a message marks it read. The more of a folder you scroll, the more loads. Messages are sent as plain text. Attachments are flagged but not downloadable yet.

The background uses the terminal's own, so it is transparent in foot. A wofi entry can run it with `foot --app-id=zoho-tui zoho`.
