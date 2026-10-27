"""Terminal Zoho Mail client."""
import re
import sys
from datetime import datetime

import html2text
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, Label, ListItem, ListView, Static, TextArea

from .api import Zoho, ZohoError, load_config

REFRESH_SECONDS = 60


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


class ComposeScreen(ModalScreen):
    """Write a new message or a reply. Dismisses with a dict or None."""

    BINDINGS = [
        Binding("ctrl+s", "send", "Send", priority=True),
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, to="", cc="", subject="", body="", title="New message"):
        super().__init__()
        self.init = dict(to=to, cc=cc, subject=subject, body=body)
        self.heading = title

    def compose(self) -> ComposeResult:
        with Vertical(id="compose"):
            yield Label(f"[b]{self.heading}[/b]   [dim]ctrl+s send · esc cancel[/dim]")
            yield Input(self.init["to"], placeholder="To", id="to")
            yield Input(self.init["cc"], placeholder="Cc", id="cc")
            yield Input(self.init["subject"], placeholder="Subject", id="subject")
            yield TextArea(self.init["body"], id="body", soft_wrap=True)

    def on_mount(self):
        target = "#body" if self.init["to"] else "#to"
        self.query_one(target).focus()
        if target == "#body":
            self.query_one(TextArea).move_cursor((0, 0))

    def action_send(self):
        to = self.query_one("#to", Input).value.strip()
        if not to:
            self.notify("Add a recipient", severity="warning")
            return
        self.dismiss(dict(
            to=to,
            cc=self.query_one("#cc", Input).value.strip(),
            subject=self.query_one("#subject", Input).value.strip(),
            body=self.query_one(TextArea).text,
        ))

    def action_cancel(self):
        self.dismiss(None)


class ZohoApp(App):
    TITLE = "Zoho Mail"
    CSS = """
    #folders { width: 22; border-right: solid $panel; }
    #messages { height: 45%; }
    #reader { border-top: solid $panel; padding: 0 1; }
    #search { dock: bottom; display: none; }
    #search.visible { display: block; }
    ComposeScreen { align: center middle; }
    #compose { width: 90%; height: 90%; border: thick $accent; background: $surface; padding: 0 1; }
    #compose TextArea { height: 1fr; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("c", "compose", "Compose"),
        Binding("r", "reply", "Reply"),
        Binding("R", "reply_all", "Reply all", show=False),
        Binding("a", "archive", "Archive"),
        Binding("d", "delete", "Delete"),
        Binding("u", "toggle_read", "Read/unread"),
        Binding("f", "toggle_flag", "Flag"),
        Binding("slash", "search", "Search"),
        Binding("g", "refresh", "Refresh"),
        Binding("j", "cursor(1)", show=False),
        Binding("k", "cursor(-1)", show=False),
        Binding("tab", "focus_next", show=False),
    ]

    def __init__(self, zoho):
        super().__init__()
        self.zoho = zoho
        self.folder = None        # current folder dict, None while showing search results
        self.rows = {}            # messageId -> message dict
        self.open_msg = None      # (message dict, details dict, body text)

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield ListView(id="folders")
            with Vertical():
                yield DataTable(id="messages", cursor_type="row", zebra_stripes=True)
                with VerticalScroll(id="reader"):
                    yield Static("", id="body")
        yield Input(placeholder="Search (enter to run, esc to close). e.g. invoice, from:bob, subject:hi", id="search")
        yield Footer()

    def on_mount(self):
        self.sub_title = self.zoho.from_address
        t = self.query_one(DataTable)
        t.add_column(" ", key="flag", width=2)
        t.add_column("From", key="from", width=26)
        t.add_column("Subject", key="subject")
        t.add_column("Date", key="date", width=9)
        self.load_folders()
        self.set_interval(REFRESH_SECONDS, self.background_refresh)

    # ---- data loading ---------------------------------------------------

    @work(thread=True, exclusive=True, group="folders")
    def load_folders(self, select_current=False):
        try:
            folders = self.zoho.folders(refresh=True)
        except ZohoError as e:
            self.call_from_thread(self.notify, str(e), severity="error")
            return
        self.call_from_thread(self._show_folders, folders, select_current)

    def _show_folders(self, folders, select_current):
        lv = self.query_one("#folders", ListView)
        index = lv.index or 0
        lv.clear()
        for f in folders:
            unread = int(f.get("unreadCount") or 0)
            label = f"{f['folderName']} [b]({unread})[/b]" if unread else f["folderName"]
            item = ListItem(Label(label))
            item.folder = f
            lv.append(item)
        if select_current:
            if self.folder:
                for i, f in enumerate(folders):
                    if f["folderId"] == self.folder["folderId"]:
                        index = i
            lv.index = index
        else:
            inbox = next((i for i, f in enumerate(folders) if f["folderName"].lower() == "inbox"), 0)
            lv.index = inbox
            self.folder = folders[inbox]
            self.load_messages()
            self.query_one(DataTable).focus()

    @on(ListView.Selected, "#folders")
    def folder_selected(self, event):
        self.folder = event.item.folder
        self.load_messages()
        self.query_one(DataTable).focus()

    @work(thread=True, exclusive=True, group="messages")
    def load_messages(self, keep_cursor=False):
        try:
            msgs = self.zoho.messages(self.folder["folderId"])
        except ZohoError as e:
            self.call_from_thread(self.notify, str(e), severity="error")
            return
        self.call_from_thread(self._show_messages, msgs, self.folder["folderName"], keep_cursor)

    @work(thread=True, exclusive=True, group="messages")
    def run_search(self, query):
        try:
            msgs = self.zoho.search(query)
        except ZohoError as e:
            self.call_from_thread(self.notify, str(e), severity="error")
            return
        self.call_from_thread(self._show_messages, msgs, f"Search: {query}", False)

    def _row(self, m):
        style = "bold" if is_unread(m) else ""
        flag = Text("★", style="yellow") if is_flagged(m) else Text("●", style="cyan") if is_unread(m) else Text("")
        sender = m.get("sender") or m.get("fromAddress", "")
        return (flag, Text(sender, style=style, overflow="ellipsis", no_wrap=True),
                Text(m.get("subject") or "(no subject)", style=style, no_wrap=True),
                Text(fmt_time(m.get("receivedTime")), style=style))

    def _show_messages(self, msgs, title, keep_cursor):
        t = self.query_one(DataTable)
        row = t.cursor_row if keep_cursor else 0
        t.clear()
        self.rows = {}
        for m in msgs:
            self.rows[m["messageId"]] = m
            t.add_row(*self._row(m), key=m["messageId"])
        self.title = f"Zoho Mail · {title}"
        if msgs:
            t.move_cursor(row=min(row, len(msgs) - 1))
        else:
            self.query_one("#body", Static).update("[dim]No messages.[/dim]")

    def background_refresh(self):
        if self.folder and not self.screen.is_modal:
            self.load_messages(keep_cursor=True)
            self.load_folders(select_current=True)

    # ---- reading ----------------------------------------------------------

    def current(self):
        t = self.query_one(DataTable)
        if not self.rows or t.cursor_row < 0:
            return None
        key = t.coordinate_to_cell_key((t.cursor_row, 0)).row_key.value
        return self.rows.get(key)

    @on(DataTable.RowHighlighted, "#messages")
    def row_highlighted(self, event):
        m = self.current()
        if m and (not self.open_msg or self.open_msg[0]["messageId"] != m["messageId"]):
            self.query_one("#body", Static).update("[dim]Loading…[/dim]")
            self.load_body(m)

    @work(thread=True, exclusive=True, group="body")
    def load_body(self, m):
        import time
        time.sleep(0.15)  # debounce fast scrolling; exclusive cancels the stale worker
        from textual.worker import get_current_worker
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
                head.append(f"{label}: ", style="bold")
                head.append(html_to_text(val).replace("\n", " ") + "\n")
        head.append("Subject: ", style="bold")
        head.append((details.get("subject") or "") + "\n")
        head.append("Date: ", style="bold")
        try:
            head.append(datetime.fromtimestamp(int(details["receivedTime"]) / 1000).strftime("%a %d %b %Y %H:%M") + "\n")
        except (KeyError, ValueError, TypeError):
            head.append("\n")
        if m.get("hasAttachment") == "1":
            head.append("📎 has attachments (open in webmail)\n", style="yellow")
        head.append("\n")
        head.append(body)
        self.query_one("#body", Static).update(head)
        self.query_one("#reader").scroll_home(animate=False)
        if is_unread(m):
            self.set_read(m, True, quiet=True)

    # ---- actions --------------------------------------------------------

    def action_cursor(self, delta):
        t = self.query_one(DataTable)
        if self.focused is t or self.focused is None:
            t.move_cursor(row=t.cursor_row + delta)
        elif isinstance(self.focused, ListView):
            self.focused.index = max(0, (self.focused.index or 0) + delta)

    def action_refresh(self):
        if self.folder:
            self.load_messages(keep_cursor=True)
        self.load_folders(select_current=True)
        self.notify("Refreshed", timeout=1.5)

    def _update_row(self, m):
        t = self.query_one(DataTable)
        for col, val in zip(("flag", "from", "subject", "date"), self._row(m)):
            t.update_cell(m["messageId"], col, val)

    def _remove_row(self, m):
        t = self.query_one(DataTable)
        t.remove_row(m["messageId"])
        self.rows.pop(m["messageId"], None)
        self.open_msg = None
        if not self.rows:
            self.query_one("#body", Static).update("[dim]No messages.[/dim]")
        elif nxt := self.current():
            self.load_body(nxt)

    @work(thread=True)
    def set_read(self, m, read, quiet=False):
        try:
            self.zoho.mark_read(m["messageId"], read)
        except ZohoError as e:
            self.call_from_thread(self.notify, str(e), severity="error")
            return
        m["status"] = "1" if read else "0"
        self.call_from_thread(self._update_row, m)
        if not quiet:
            self.call_from_thread(self.notify, "Marked read" if read else "Marked unread", timeout=1.5)
        self.load_folders(select_current=True)

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
            self.call_from_thread(self.notify, str(e), severity="error")
            return
        m["flagid"] = "important" if on_ else "flag_not_set"
        self.call_from_thread(self._update_row, m)

    @work(thread=True)
    def action_archive(self):
        m = self.current()
        if not m:
            return
        try:
            self.zoho.move(m["messageId"], self.zoho.folder_id("Archive"))
        except ZohoError as e:
            self.call_from_thread(self.notify, str(e), severity="error")
            return
        self.call_from_thread(self._remove_row, m)
        self.call_from_thread(self.notify, "Archived", timeout=1.5)
        self.load_folders(select_current=True)

    @work(thread=True)
    def action_delete(self):
        m = self.current()
        if not m:
            return
        try:
            self.zoho.delete(m["folderId"], m["messageId"])
        except ZohoError as e:
            self.call_from_thread(self.notify, str(e), severity="error")
            return
        self.call_from_thread(self._remove_row, m)
        self.call_from_thread(self.notify, "Moved to Trash", timeout=1.5)
        self.load_folders(select_current=True)

    def action_search(self):
        s = self.query_one("#search", Input)
        s.add_class("visible")
        s.focus()

    @on(Input.Submitted, "#search")
    def search_submitted(self, event):
        q = event.value.strip()
        event.input.remove_class("visible")
        self.query_one(DataTable).focus()
        if q:
            self.folder = None
            self.run_search(q)

    def on_key(self, event):
        s = self.query_one("#search", Input)
        if event.key == "escape" and s.has_class("visible"):
            s.remove_class("visible")
            self.query_one(DataTable).focus()
            event.stop()

    def check_action(self, action, parameters):
        # Let single-letter keys type into inputs instead of firing actions.
        if isinstance(self.focused, (Input, TextArea)) and action not in ("focus_next",):
            return False
        return True

    def action_compose(self):
        self.push_screen(ComposeScreen(), self._send_new)

    def _reply(self, all_):
        if not self.open_msg:
            self.notify("Open a message first", severity="warning")
            return
        m, details, body = self.open_msg
        sender = addresses(details.get("fromAddress"))
        to = ", ".join(sender)
        cc = ""
        if all_:
            others = addresses(details.get("toAddress")) + addresses(details.get("ccAddress"))
            mine = self.zoho.from_address.lower()
            cc = ", ".join(a for a in dict.fromkeys(others) if a.lower() != mine and a not in sender)
        subject = details.get("subject") or ""
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        when = fmt_time(details.get("receivedTime"))
        quoted = "\n".join("> " + line for line in body.splitlines())
        text = f"\n\nOn {when}, {to} wrote:\n{quoted}\n"
        self.push_screen(ComposeScreen(to, cc, subject, text, title="Reply"),
                         lambda r: self._send_reply(m, r))

    def action_reply(self):
        self._reply(False)

    def action_reply_all(self):
        self._reply(True)

    @work(thread=True)
    def _send_new(self, r):
        if not r:
            return
        try:
            self.zoho.send(r["to"], r["subject"], r["body"], cc=r["cc"])
        except ZohoError as e:
            self.call_from_thread(self.notify, f"Send failed: {e}", severity="error")
            return
        self.call_from_thread(self.notify, "Sent")

    @work(thread=True)
    def _send_reply(self, m, r):
        if not r:
            return
        try:
            self.zoho.reply(m["messageId"], r["to"], r["subject"], r["body"], cc=r["cc"])
        except ZohoError as e:
            self.call_from_thread(self.notify, f"Reply failed: {e}", severity="error")
            return
        self.call_from_thread(self.notify, "Reply sent")


def main():
    try:
        zoho = Zoho(load_config())
    except ZohoError as e:
        sys.exit(f"zoho-tui: {e}")
    except Exception as e:
        sys.exit(f"zoho-tui: could not connect: {e}")
    ZohoApp(zoho).run()
