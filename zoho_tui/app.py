"""Keyboard-driven terminal Zoho Mail client."""
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime

import html

import html2text
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Input, Label, OptionList, Static, TextArea
from textual.widgets.option_list import Option
from textual.worker import get_current_worker

from .api import Zoho, ZohoError, load_config

REFRESH_SECONDS = 60
PAGE_SIZE = 50

# g-prefix jumps: second key -> folder name
GOTO = {"i": "Inbox", "a": "Archive", "s": "Sent", "d": "Drafts", "t": "Trash", "j": "Spam"}

HELP = """\
[b]Move[/b]
  j / k            down / up
  ctrl+d / ctrl+u  half page down / up
  gg / G           top / bottom
  n / N            next / previous unread
  h / l, ←/→, tab  switch pane (folders, list, message)
  enter            open folder / read message full screen
  esc / q          leave full screen
  space / b        scroll message down / up (from the list)

[b]Go to[/b]
  gi gs gd ga gt gj  Inbox, Sent, Drafts, Archive, Trash, Spam
  gf               folder picker
  /                search (word prefixes, or Zoho syntax like sender:bob)

[b]Act[/b]
  c                compose
  r / R            reply / reply all
  F                forward
  a                archive
  d                delete (to Trash)
  z                undo last archive / delete
  u                toggle read
  s                toggle star (flag)
  o                open a link from the message
  y                copy sender address
  ctrl+r           refresh
  ?                this help
  Q                quit

[b]Compose[/b]
  tab / shift+tab  next / previous field
  ctrl+e           edit body in $EDITOR
  ctrl+s           send
  esc              cancel (twice if you've typed something)
"""


def html_to_text(html):
    h = html2text.HTML2Text()
    h.body_width = 0
    h.ignore_images = True
    h.ignore_emphasis = True
    h.inline_links = False
    return h.handle(html).strip()


def addresses(raw):
    """Pull bare email addresses out of Zoho's escaped 'Name <addr>' strings."""
    if not raw or raw == "Not Provided":
        return []
    return re.findall(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+", html_to_text(raw))


def links(text):
    return list(dict.fromkeys(re.findall(r"https?://[^\s<>\"')\]]+", text)))


def fmt_time(ms):
    try:
        dt = datetime.fromtimestamp(int(ms) / 1000)
    except (TypeError, ValueError):
        return ""
    now = datetime.now()
    if dt.date() == now.date():
        return dt.strftime("%H:%M")
    if dt.year == now.year:
        return dt.strftime("%d %b")
    return dt.strftime("%d/%m/%y")


def is_unread(m):
    return str(m.get("status")) == "0"


def is_flagged(m):
    return m.get("flagid", "flag_not_set") != "flag_not_set"


def copy(text):
    try:
        subprocess.run(["wl-copy"], input=text.encode(), check=True, timeout=3)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


# ---- popups ---------------------------------------------------------------


class HelpScreen(ModalScreen):
    BINDINGS = [Binding("escape,q,question_mark", "close", "Close")]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help", classes="popup") as v:
            v.border_title = "Keys"
            v.border_subtitle = "esc to close"
            yield Static(HELP)

    def on_mount(self):
        self.query_one("#help").focus()

    def action_close(self):
        self.dismiss()


class PickerScreen(ModalScreen):
    """Filterable list. Dismisses with the chosen option id, or None."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("down,ctrl+n,ctrl+j", "move(1)", show=False, priority=True),
        Binding("up,ctrl+p,ctrl+k", "move(-1)", show=False, priority=True),
    ]

    def __init__(self, title, items):
        super().__init__()
        self.title_text = title
        self.items = items  # list of (id, label)

    def compose(self) -> ComposeResult:
        with Vertical(id="picker", classes="popup") as v:
            v.border_title = self.title_text
            v.border_subtitle = "type to filter · enter pick · esc close"
            yield Input(placeholder="filter")
            yield OptionList()

    def on_mount(self):
        self.fill("")
        self.query_one(Input).focus()

    def fill(self, q):
        ol = self.query_one(OptionList)
        ol.clear_options()
        q = q.lower()
        ol.add_options(Option(label, id=i) for i, label in self.items if q in Text.from_markup(label).plain.lower())
        if ol.option_count:
            ol.highlighted = 0

    @on(Input.Changed)
    def changed(self, event):
        self.fill(event.value)

    def action_move(self, d):
        ol = self.query_one(OptionList)
        if ol.option_count:
            ol.highlighted = ((ol.highlighted or 0) + d) % ol.option_count

    @on(Input.Submitted)
    def submitted(self):
        ol = self.query_one(OptionList)
        if ol.highlighted is not None:
            self.dismiss(ol.get_option_at_index(ol.highlighted).id)

    @on(OptionList.OptionSelected)
    def selected(self, event):
        self.dismiss(event.option.id)

    def action_cancel(self):
        self.dismiss(None)


class SearchScreen(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="searchbox", classes="popup") as v:
            v.border_title = "Search"
            v.border_subtitle = "enter search · esc close"
            yield Input(placeholder="words, from:bob, subject:invoice")

    def on_mount(self):
        self.query_one(Input).focus()

    @on(Input.Submitted)
    def submitted(self, event):
        self.dismiss(event.value.strip() or None)

    def action_cancel(self):
        self.dismiss(None)


class ComposeScreen(ModalScreen):
    """Write a new message, reply or forward. Dismisses with a dict or None."""

    BINDINGS = [
        Binding("ctrl+s", "send", "Send", priority=True),
        Binding("ctrl+e", "editor", "Editor", priority=True),
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, to="", cc="", subject="", body="", title="New message"):
        super().__init__()
        self.init = dict(to=to, cc=cc, subject=subject, body=body)
        self.heading = title
        self.armed_cancel = False

    def compose(self) -> ComposeResult:
        with Vertical(id="compose", classes="popup") as v:
            v.border_title = self.heading
            v.border_subtitle = "tab next field · ctrl+e $EDITOR · ctrl+s send · esc cancel"
            for key, label in (("to", "To"), ("cc", "Cc"), ("subject", "Subject")):
                with Horizontal(classes="field"):
                    yield Label(f"{label:<8}")
                    yield Input(self.init[key], id=key, compact=True)
            yield TextArea(self.init["body"], id="body", soft_wrap=True, compact=True)

    def on_mount(self):
        if self.init["to"]:
            self.query_one(TextArea).focus()
            self.query_one(TextArea).move_cursor((0, 0))
        else:
            self.query_one("#to").focus()

    def values(self):
        return dict(
            to=self.query_one("#to", Input).value.strip(),
            cc=self.query_one("#cc", Input).value.strip(),
            subject=self.query_one("#subject", Input).value.strip(),
            body=self.query_one(TextArea).text,
        )

    def action_send(self):
        v = self.values()
        if not v["to"]:
            self.notify("Add a recipient", severity="warning")
            self.query_one("#to").focus()
            return
        self.dismiss(v)

    def action_editor(self):
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
        with tempfile.NamedTemporaryFile("w+", suffix=".eml.txt", delete=False) as f:
            f.write(self.query_one(TextArea).text)
            path = f.name
        try:
            with self.app.suspend():
                subprocess.run([*editor.split(), path])
            with open(path) as f:
                self.query_one(TextArea).text = f.read()
        finally:
            os.unlink(path)
        self.query_one(TextArea).focus()

    def action_cancel(self):
        v = self.values()
        dirty = any(v[k] != (self.init[k] if k == "body" else self.init[k].strip()) for k in v)
        if dirty and not self.armed_cancel:
            self.armed_cancel = True
            self.notify("Press esc again to discard", timeout=2)
            self.set_timer(2, lambda: setattr(self, "armed_cancel", False))
            return
        self.dismiss(None)


# ---- main app -------------------------------------------------------------


class Reader(VerticalScroll):
    ALLOW_MAXIMIZE = True



class ZohoApp(App):
    TITLE = "Zoho Mail"
    ENABLE_COMMAND_PALETTE = False
    # Terminal colours only, so it follows whatever palette the terminal uses.
    CSS = """
    Screen { background: ansi_default; }
    .pane { border: round ansi_bright_black; background: ansi_default; border-title-color: ansi_bright_black; }
    .pane.active { border: round ansi_white; border-title-color: ansi_white; border-title-style: bold; }
    #top { height: 45%; }
    #folders { width: 24; height: 1fr; }
    #messages { width: 1fr; height: 1fr; }
    #reader { height: 1fr; padding: 0 1; scrollbar-size-vertical: 1; }
    * { scrollbar-background: ansi_default; scrollbar-background-hover: ansi_default; scrollbar-background-active: ansi_default;
        scrollbar-color: ansi_bright_black; scrollbar-color-hover: ansi_bright_black; scrollbar-color-active: ansi_white; }
    #status { height: 1; padding: 0 1; color: ansi_bright_black; background: ansi_default; }
    DataTable, OptionList { background: ansi_default; }
    DataTable > .datatable--header { background: ansi_default; color: ansi_bright_black; text-style: none; }
    DataTable > .datatable--cursor, DataTable:focus > .datatable--cursor { background: ansi_bright_black; color: ansi_default; text-style: none; }
    #messages.active > .datatable--cursor { background: ansi_white; color: ansi_black; }
    OptionList > .option-list--option-highlighted, OptionList:focus > .option-list--option-highlighted
        { background: ansi_bright_black; color: ansi_default; text-style: none; }
    .active > .option-list--option-highlighted, .popup OptionList > .option-list--option-highlighted
        { background: ansi_white; color: ansi_black; }
    ModalScreen { align: center middle; background: transparent; }
    .popup { border: round ansi_white; background: ansi_default; border-title-color: ansi_white; border-title-style: bold;
             border-subtitle-color: ansi_bright_black; padding: 0 1; }
    Input, TextArea { background: ansi_default; }
    LoadingIndicator { background: ansi_default; color: ansi_white; }
    #help { width: 64; height: 80%; }
    #picker { width: 50; height: 60%; }
    #picker Input, #searchbox Input { border: none; height: 1; padding: 0; margin-bottom: 1; }
    #picker OptionList { border: none; height: 1fr; padding: 0; }
    #searchbox { width: 70; height: 3; }
    #compose { width: 90%; height: 90%; }
    #compose .field { height: 1; }
    #compose .field Label { width: 9; color: ansi_bright_black; }
    #compose .field Input { width: 1fr; }
    #compose TextArea { height: 1fr; margin-top: 1; border-top: solid ansi_bright_black; }
    """
    BINDINGS = [
        Binding("Q", "quit", "Quit"),
        Binding("question_mark", "help"),
        Binding("q,escape", "back", show=False),
        Binding("j,down", "move(1)", show=False),
        Binding("k,up", "move(-1)", show=False),
        Binding("ctrl+d", "move_half(1)", show=False),
        Binding("ctrl+u", "move_half(-1)", show=False),
        Binding("G,end", "bottom", show=False),
        Binding("home", "top", show=False),
        Binding("g", "g_prefix", show=False),
        Binding("h,left,shift+tab", "pane(-1)", show=False),
        Binding("l,right,tab", "pane(1)", show=False),
        Binding("space", "scroll_reader(1)", show=False),
        Binding("b", "scroll_reader(-1)", show=False),
        Binding("n", "next_unread(1)", show=False),
        Binding("N", "next_unread(-1)", show=False),
        Binding("enter", "open", show=False),
        Binding("c", "compose"),
        Binding("r", "reply(False)"),
        Binding("R", "reply(True)"),
        Binding("F", "forward"),
        Binding("a", "archive"),
        Binding("d", "delete"),
        Binding("z", "undo"),
        Binding("u", "toggle_read"),
        Binding("s", "toggle_flag"),
        Binding("o", "open_link"),
        Binding("y", "yank"),
        Binding("slash", "search"),
        Binding("ctrl+r", "refresh"),
    ]

    def __init__(self, zoho):
        super().__init__(ansi_color=True)  # keep the terminal's own (transparent) background
        self.zoho = zoho
        self.folder = None        # current folder dict, None while showing search results
        self.view_name = ""
        self.rows = {}            # messageId -> message dict
        self.order = []           # messageIds in table order
        self.open_msg = None      # (message dict, details dict, body text)
        self.g_pending = False
        self.undo_stack = []      # (message dict, original folderId)
        self.more = False         # more pages available in the current folder
        self.loading_more = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="top"):
            folders = OptionList(id="folders", classes="pane")
            folders.border_title = "Folders"
            yield folders
            table = DataTable(id="messages", classes="pane", cursor_type="row")
            table.border_title = "Inbox"
            yield table
        reader = Reader(id="reader", classes="pane")
        reader.border_title = "Message"
        with reader:
            yield Static("", id="body")
        yield Static("", id="status")

    def on_mount(self):
        t = self.query_one(DataTable)
        t.add_column(" ", key="flag", width=2)
        t.add_column("From", key="from", width=24)
        t.add_column("Subject", key="subject")
        t.add_column("Date", key="date", width=7)
        t.focus()
        self.load_folders()
        self.set_interval(REFRESH_SECONDS, self.background_refresh)
        self.update_status()

    def on_descendant_focus(self, event):
        # Track the active pane with a class instead of :focus, which Textual clears
        # whenever the terminal window loses focus (e.g. the mouse leaving it).
        if event.widget.id in self.PANES:
            for pid in self.PANES:
                self.query_one("#" + pid).set_class(pid == event.widget.id, "active")

    # ---- status line ----------------------------------------------------

    def notify(self, message, *, title="", severity="information", timeout=None, markup=True):
        """Show messages in the status line instead of Textual's toast popups."""
        self.flash_id = getattr(self, "flash_id", 0) + 1
        self.flash = (Text(str(message)).markup, severity)
        self.update_status()
        token = self.flash_id
        self.set_timer(timeout or (6 if severity == "error" else 3), lambda: self._clear_flash(token))

    def _clear_flash(self, token):
        if token == self.flash_id:
            self.flash = None
            self.update_status()

    def update_status(self):
        parts = [f"[b]{self.zoho.from_address}[/b]", self.view_name or "…"]
        if self.rows:
            unread = sum(is_unread(m) for m in self.rows.values())
            parts.append(f"{len(self.rows)}{'+' if self.more else ''} messages, {unread} unread")
        if self.g_pending:
            parts.append("[reverse] g- [/reverse]")
        flash = getattr(self, "flash", None)
        if flash:
            msg, severity = flash
            color = {"error": "ansi_red", "warning": "ansi_yellow"}.get(severity, "ansi_default")
            parts.append(f"[{color}]{msg}[/]")
        else:
            parts.append("? help")
        self.query_one("#status", Static).update("  ·  ".join(parts))

    # ---- key gating -------------------------------------------------------

    def check_action(self, action, parameters):
        if self.g_pending and action != "g_prefix":
            return False
        if isinstance(self.focused, (Input, TextArea)):
            return False
        return True

    def on_key(self, event):
        if not self.g_pending:
            return
        self.g_pending = False
        event.stop()
        event.prevent_default()
        k = event.character
        if k == "g":
            self.action_top()
        elif k == "f":
            self.action_pick_folder()
        elif k in GOTO:
            self.goto_folder(GOTO[k])
        self.update_status()

    def action_g_prefix(self):
        if self.g_pending:  # "gg"
            self.g_pending = False
            self.action_top()
        else:
            self.g_pending = True
        self.update_status()

    # ---- folders ----------------------------------------------------------

    @work(thread=True, exclusive=True, group="folders")
    def load_folders(self, initial=True):
        try:
            folders = self.zoho.folders(refresh=True)
        except ZohoError as e:
            self.call_from_thread(self.notify, str(e), severity="error")
            return
        self.call_from_thread(self._show_folders, folders, initial)

    def _show_folders(self, folders, initial):
        ol = self.query_one("#folders", OptionList)
        hl = ol.highlighted
        ol.clear_options()
        for f in folders:
            unread = int(f.get("unreadCount") or 0)
            label = f"{f['folderName']}  [b]({unread})[/b]" if unread else f["folderName"]
            ol.add_option(Option(label, id=f["folderId"]))
        if initial:
            self.goto_folder("Inbox")
        elif hl is not None and hl < ol.option_count:
            ol.highlighted = hl

    def goto_folder(self, name_or_id):
        for f in self.zoho.folders():
            if f["folderId"] == name_or_id or f["folderName"].lower() == str(name_or_id).lower():
                self.folder = f
                ol = self.query_one("#folders", OptionList)
                try:
                    ol.highlighted = ol.get_option_index(f["folderId"])
                except Exception:
                    pass
                self.load_messages()
                self.query_one(DataTable).focus()
                return
        self.notify(f"No folder named {name_or_id}", severity="warning")

    @on(OptionList.OptionSelected, "#folders")
    def folder_selected(self, event):
        self.goto_folder(event.option.id)

    def action_pick_folder(self):
        items = []
        for f in self.zoho.folders():
            unread = int(f.get("unreadCount") or 0)
            items.append((f["folderId"], f"{f['folderName']}  [b]({unread})[/b]" if unread else f["folderName"]))
        self.push_screen(PickerScreen("Go to folder", items), lambda fid: fid and self.goto_folder(fid))

    # ---- message list -----------------------------------------------------

    @work(thread=True, exclusive=True, group="messages")
    def load_messages(self, keep_cursor=False):
        folder = self.folder
        if not keep_cursor:
            self.call_from_thread(setattr, self.query_one(DataTable), "loading", True)
        try:
            msgs = self.zoho.messages(folder["folderId"], limit=PAGE_SIZE)
        except ZohoError as e:
            self.call_from_thread(setattr, self.query_one(DataTable), "loading", False)
            self.call_from_thread(self.notify, str(e), severity="error")
            return
        self.call_from_thread(setattr, self.query_one(DataTable), "loading", False)
        self.call_from_thread(self._show_messages, msgs, folder["folderName"], keep_cursor, len(msgs) == PAGE_SIZE)

    @work(thread=True, exclusive=True, group="messages")
    def run_search(self, query):
        self.call_from_thread(self._set_loading, True, f"Searching for “{query}”…")
        start = time.monotonic()
        try:
            msgs = self.zoho.search(query, limit=PAGE_SIZE)
        except ZohoError as e:
            self.call_from_thread(self._set_loading, False)
            self.call_from_thread(self.notify, f"Search failed: {e}", severity="error")
            return
        self.call_from_thread(self._set_loading, False)
        self.call_from_thread(self._show_messages, msgs, f"Search: {query}", False, False)
        n = len(msgs)
        found = f"{n}{'+' if n == PAGE_SIZE else ''} result{'s' if n != 1 else ''}" if n else "No results"
        self.call_from_thread(self.notify, f"{found} for “{query}” ({time.monotonic() - start:.1f}s)",
                              severity="information" if n else "warning", timeout=5)

    def _set_loading(self, on_, message=""):
        self.query_one(DataTable).loading = on_
        if on_:
            self.notify(message, timeout=60)

    @work(thread=True, group="more")
    def load_more(self):
        folder = self.folder
        try:
            msgs = self.zoho.messages(folder["folderId"], start=len(self.order) + 1, limit=PAGE_SIZE)
        except ZohoError as e:
            self.call_from_thread(self.notify, str(e), severity="error")
            msgs = None
        self.call_from_thread(self._append_messages, msgs, folder)

    def _append_messages(self, msgs, folder):
        self.loading_more = False
        if msgs is None or folder is not self.folder:
            return
        t = self.query_one(DataTable)
        for m in msgs:
            if m["messageId"] not in self.rows:
                self.rows[m["messageId"]] = m
                self.order.append(m["messageId"])
                t.add_row(*self._row(m), key=m["messageId"])
        self.more = len(msgs) == PAGE_SIZE
        self.update_status()

    def _row(self, m):
        style = "bold" if is_unread(m) else ""
        mark = Text("★", style="yellow") if is_flagged(m) else Text("•", style="bold") if is_unread(m) else Text("")
        sender = html.unescape(m.get("sender") or m.get("fromAddress", ""))
        return (mark, Text(sender, style=style, overflow="ellipsis", no_wrap=True),
                Text(html.unescape(m.get("subject") or "(no subject)"), style=style, no_wrap=True, overflow="ellipsis"),
                Text(fmt_time(m.get("receivedTime")), style=style or "dim"))

    def _show_messages(self, msgs, title, keep_cursor, more):
        t = self.query_one(DataTable)
        keep_id = self.order[t.cursor_row] if keep_cursor and 0 <= t.cursor_row < len(self.order) else None
        t.clear()
        self.rows, self.order = {}, []
        for m in msgs:
            self.rows[m["messageId"]] = m
            self.order.append(m["messageId"])
            t.add_row(*self._row(m), key=m["messageId"])
        self.view_name = title
        self.more = more
        t.border_title = title
        row = self.order.index(keep_id) if keep_id in self.order else 0
        if msgs:
            t.move_cursor(row=row)
            if not keep_cursor:
                self.open_msg = None
                self.load_body(self.rows[self.order[row]])
        else:
            self.open_msg = None
            self.query_one("#body", Static).update("[dim]No messages.[/dim]")
            self.query_one("#reader").border_title = "Message"
        self.update_status()

    def background_refresh(self):
        if self.folder and not isinstance(self.screen, ModalScreen):
            self.load_messages(keep_cursor=True)
            self.load_folders(initial=False)

    def current(self):
        t = self.query_one(DataTable)
        if not self.order or not 0 <= t.cursor_row < len(self.order):
            return None
        return self.rows.get(self.order[t.cursor_row])

    @on(DataTable.RowHighlighted, "#messages")
    def row_highlighted(self, event):
        m = self.current()
        if not m:
            return
        if not self.open_msg or self.open_msg[0]["messageId"] != m["messageId"]:
            self.load_body(m)
        if self.more and not self.loading_more and event.cursor_row >= len(self.order) - 5:
            self.loading_more = True
            self.load_more()

    # ---- reader -----------------------------------------------------------

    @work(thread=True, exclusive=True, group="body")
    def load_body(self, m):
        time.sleep(0.12)  # debounce fast scrolling; exclusive cancels the stale worker
        if get_current_worker().is_cancelled:
            return
        try:
            details = self.zoho.details(m["folderId"], m["messageId"])
            body = html_to_text(self.zoho.content(m["folderId"], m["messageId"]))
        except ZohoError as e:
            self.call_from_thread(self.notify, str(e), severity="error")
            return
        if get_current_worker().is_cancelled:
            return
        self.call_from_thread(self._show_body, m, details, body)

    def _show_body(self, m, details, body):
        self.open_msg = (m, details, body)
        head = Text()
        for label, key in (("From", "fromAddress"), ("To", "toAddress"), ("Cc", "ccAddress")):
            val = details.get(key)
            if val and val != "Not Provided":
                head.append(f"{label:<8}", style="bold")
                head.append(html_to_text(val).replace("\n", " ") + "\n")
        head.append(f"{'Date':<8}", style="bold")
        try:
            head.append(datetime.fromtimestamp(int(details["receivedTime"]) / 1000).strftime("%a %d %b %Y %H:%M") + "\n")
        except (KeyError, ValueError, TypeError):
            head.append("\n")
        if m.get("hasAttachment") == "1":
            head.append("📎 has attachments (open in webmail)\n", style="yellow")
        head.append("\n")
        head.append(body)
        self.query_one("#body", Static).update(head)
        reader = self.query_one("#reader")
        reader.scroll_home(animate=False)
        reader.border_title = Text(html.unescape(details.get("subject") or "(no subject)")).markup
        if is_unread(m):
            self.set_read(m, True, quiet=True)

    # ---- navigation -------------------------------------------------------

    PANES = ("folders", "messages", "reader")

    def action_pane(self, d):
        if self.screen.maximized:
            return
        fid = self.focused.id if self.focused else "messages"
        cur = self.PANES.index(fid) if fid in self.PANES else 1
        self.query_one("#" + self.PANES[(cur + d) % 3]).focus()

    def action_move(self, d):
        f = self.focused
        if isinstance(f, DataTable):
            f.move_cursor(row=max(0, f.cursor_row + d))
        elif isinstance(f, OptionList):
            if f.option_count:
                f.highlighted = max(0, min(f.option_count - 1, (f.highlighted or 0) + d))
        elif f is not None and f.id == "reader":
            f.scroll_relative(y=d * 2, animate=False)

    def action_move_half(self, d):
        f = self.focused
        if isinstance(f, DataTable):
            f.move_cursor(row=max(0, f.cursor_row + d * max(1, f.size.height // 2)))
        else:
            self.action_scroll_reader(d)

    def action_top(self):
        f = self.focused
        if isinstance(f, DataTable):
            f.move_cursor(row=0)
        elif isinstance(f, OptionList) and f.option_count:
            f.highlighted = 0
        else:
            self.query_one("#reader").scroll_home(animate=False)

    def action_bottom(self):
        f = self.focused
        if isinstance(f, DataTable):
            f.move_cursor(row=len(self.order) - 1)
        elif isinstance(f, OptionList) and f.option_count:
            f.highlighted = f.option_count - 1
        else:
            self.query_one("#reader").scroll_end(animate=False)

    def action_scroll_reader(self, d):
        r = self.query_one("#reader")
        r.scroll_relative(y=d * max(1, r.size.height - 2), animate=False)

    def action_next_unread(self, d):
        t = self.query_one(DataTable)
        i = t.cursor_row + d
        while 0 <= i < len(self.order):
            if is_unread(self.rows[self.order[i]]):
                t.move_cursor(row=i)
                t.focus()
                return
            i += d
        self.notify("No more unread", timeout=1.5)

    def action_open(self):
        f = self.focused
        if isinstance(f, OptionList):
            if f.highlighted is not None:
                self.goto_folder(f.get_option_at_index(f.highlighted).id)
        elif isinstance(f, DataTable) and self.current():
            self.read_fullscreen()

    @on(DataTable.RowSelected, "#messages")
    def row_selected(self):
        self.read_fullscreen()

    def read_fullscreen(self):
        reader = self.query_one("#reader")
        self.screen.maximize(reader)
        reader.focus()

    def action_back(self):
        if self.screen.maximized:
            self.screen.minimize()
            self.query_one(DataTable).focus()
        elif self.folder is None:  # leave search results
            self.goto_folder("Inbox")

    def action_help(self):
        self.push_screen(HelpScreen())

    def action_search(self):
        def done(q):
            if q:
                self.folder = None
                self.run_search(q)
                self.query_one(DataTable).focus()
        self.push_screen(SearchScreen(), done)

    def action_refresh(self):
        if self.folder:
            self.load_messages(keep_cursor=True)
        self.load_folders(initial=False)
        self.notify("Refreshed", timeout=1.5)

    # ---- message actions --------------------------------------------------

    def _update_row(self, m):
        if m["messageId"] in self.rows:
            t = self.query_one(DataTable)
            for col, val in zip(("flag", "from", "subject", "date"), self._row(m)):
                t.update_cell(m["messageId"], col, val)
        self.update_status()

    def _remove_row(self, m):
        if m["messageId"] not in self.rows:
            return
        self.query_one(DataTable).remove_row(m["messageId"])
        self.rows.pop(m["messageId"], None)
        self.order.remove(m["messageId"])
        self.open_msg = None
        if self.screen.maximized:
            self.action_back()
        if not self.rows:
            self.query_one("#body", Static).update("[dim]No messages.[/dim]")
        elif nxt := self.current():
            self.load_body(nxt)
        self.update_status()

    def _err(self, e, prefix=""):
        self.call_from_thread(self.notify, f"{prefix}{e}", severity="error")

    @work(thread=True)
    def set_read(self, m, read, quiet=False):
        try:
            self.zoho.mark_read(m["messageId"], read)
        except ZohoError as e:
            return self._err(e)
        m["status"] = "1" if read else "0"
        self.call_from_thread(self._update_row, m)
        if not quiet:
            self.call_from_thread(self.notify, "Marked read" if read else "Marked unread", timeout=1.5)
        self.load_folders(initial=False)

    def action_toggle_read(self):
        if m := self.current():
            self.set_read(m, is_unread(m))

    @work(thread=True)
    def action_toggle_flag(self):
        m = self.current()
        if not m:
            return
        on_ = not is_flagged(m)
        try:
            self.zoho.flag(m["messageId"], on_)
        except ZohoError as e:
            return self._err(e)
        m["flagid"] = "important" if on_ else "flag_not_set"
        self.call_from_thread(self._update_row, m)

    @work(thread=True)
    def _move_away(self, m, how, origin):
        try:
            if how == "archive":
                self.zoho.move(m["messageId"], self.zoho.folder_id("Archive"))
            else:
                self.zoho.delete(m["folderId"], m["messageId"])
        except ZohoError as e:
            return self._err(e)
        self.undo_stack.append((m, origin))
        self.call_from_thread(self.notify, ("Archived" if how == "archive" else "Moved to Trash") + "  (z to undo)", timeout=3)
        self.load_folders(initial=False)

    def _act_away(self, how):
        m = self.current()
        if not m:
            return
        self._remove_row(m)  # optimistic, so rapid a/d presses each hit the next message
        self._move_away(m, how, m["folderId"])

    def action_archive(self):
        self._act_away("archive")

    def action_delete(self):
        self._act_away("delete")

    @work(thread=True)
    def action_undo(self):
        if not self.undo_stack:
            self.call_from_thread(self.notify, "Nothing to undo", timeout=1.5)
            return
        m, folder_id = self.undo_stack.pop()
        try:
            self.zoho.move(m["messageId"], folder_id)
        except ZohoError as e:
            return self._err(e, "Undo failed: ")
        self.call_from_thread(self.notify, "Restored", timeout=1.5)
        if self.folder and self.folder["folderId"] == folder_id:
            self.call_from_thread(self.load_messages, True)
        self.load_folders(initial=False)

    def action_yank(self):
        if not self.open_msg:
            return
        addr = ", ".join(addresses(self.open_msg[1].get("fromAddress")))
        if addr and copy(addr):
            self.notify(f"Copied {addr}", timeout=1.5)

    def action_open_link(self):
        if not self.open_msg:
            return
        found = links(self.open_msg[2])
        if not found:
            self.notify("No links in this message", timeout=1.5)
            return
        items = [(str(i), Text(u).markup) for i, u in enumerate(found)]

        def done(i):
            if i is not None:
                subprocess.Popen(["xdg-open", found[int(i)]], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        self.push_screen(PickerScreen(f"Links ({len(found)})", items), done)

    # ---- compose / reply / forward ---------------------------------------

    def _quote(self, details, body):
        sender = ", ".join(addresses(details.get("fromAddress")))
        when = fmt_time(details.get("receivedTime"))
        return f"\n\nOn {when}, {sender} wrote:\n" + "\n".join("> " + l for l in body.splitlines()) + "\n"

    def action_compose(self):
        self.push_screen(ComposeScreen(), lambda r: r and self._send("send", None, r))

    def action_reply(self, all_):
        if not self.open_msg:
            return
        m, details, body = self.open_msg
        sender = addresses(details.get("fromAddress"))
        cc = ""
        if all_:
            mine = self.zoho.from_address.lower()
            others = addresses(details.get("toAddress")) + addresses(details.get("ccAddress"))
            cc = ", ".join(a for a in dict.fromkeys(others) if a.lower() != mine and a not in sender)
        subject = html.unescape(details.get("subject") or "")
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        self.push_screen(
            ComposeScreen(", ".join(sender), cc, subject, self._quote(details, body),
                          title="Reply all" if all_ else "Reply"),
            lambda r: r and self._send("reply", m, r))

    def action_forward(self):
        if not self.open_msg:
            return
        m, details, body = self.open_msg
        subject = html.unescape(details.get("subject") or "")
        if not subject.lower().startswith("fwd:"):
            subject = f"Fwd: {subject}"
        fwd = (f"\n\n---------- Forwarded message ----------\n"
               f"From: {', '.join(addresses(details.get('fromAddress')))}\n"
               f"Subject: {html.unescape(details.get('subject') or '')}\n\n{body}\n")
        self.push_screen(ComposeScreen("", "", subject, fwd, title="Forward"),
                         lambda r: r and self._send("forward", m, r))

    @work(thread=True)
    def _send(self, kind, m, r):
        try:
            if kind == "send":
                self.zoho.send(r["to"], r["subject"], r["body"], cc=r["cc"])
            elif kind == "reply":
                self.zoho.reply(m["messageId"], r["to"], r["subject"], r["body"], cc=r["cc"])
            else:
                self.zoho.forward(m["messageId"], r["to"], r["subject"], r["body"], cc=r["cc"])
        except ZohoError as e:
            return self._err(e, "Send failed: ")
        self.call_from_thread(self.notify, {"send": "Sent", "reply": "Reply sent", "forward": "Forwarded"}[kind])


def main():
    try:
        zoho = Zoho(load_config())
    except ZohoError as e:
        sys.exit(f"zoho-tui: {e}")
    except Exception as e:
        sys.exit(f"zoho-tui: could not connect: {e}")
    ZohoApp(zoho).run(mouse=False)
