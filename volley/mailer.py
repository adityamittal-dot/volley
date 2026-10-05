"""Gmail over SMTP (send) and IMAP (drafts, sent detection, replies, bounces).

Uses a Gmail app password, so there's no OAuth app to register. Gmail's
X-GM-RAW search extension lets us run normal Gmail search queries over IMAP.
"""
import imaplib
import os
import re
import smtplib
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

FREEMAIL = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "proton.me", "protonmail.com", "icloud.com"}


def creds():
    """GMAIL_ADDRESS logs in; SEND_AS (optional) is a "Send mail as" alias configured in that Gmail."""
    addr, pw = os.environ.get("GMAIL_ADDRESS"), os.environ.get("GMAIL_APP_PASSWORD")
    if not addr or not pw:
        raise RuntimeError("GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be set")
    return addr, pw.replace(" ", "")


def build(sender_name, to, subject, body, reply_to_id=None, attachment=None, extra=()):
    """`attachment` is the resume (sent as <Name>_Resume.pdf); `extra` are more PDFs sent under their own names."""
    addr, _ = creds()
    msg = EmailMessage()
    msg["From"] = f"{sender_name} <{os.environ.get('SEND_AS') or addr}>"
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=addr.split("@")[1])
    if reply_to_id:
        msg["In-Reply-To"] = reply_to_id
        msg["References"] = reply_to_id
    msg.set_content(body)
    if attachment:
        with open(attachment, "rb") as f:
            msg.add_attachment(f.read(), maintype="application", subtype="pdf",
                               filename=f"{sender_name.replace(' ', '_')}_Resume.pdf")
    for path in extra:
        with open(path, "rb") as f:
            msg.add_attachment(f.read(), maintype="application", subtype="pdf", filename=os.path.basename(path))
    return msg


def send(msg):
    addr, pw = creds()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as smtp:
        smtp.login(addr, pw)
        smtp.send_message(msg)
    return msg["Message-ID"]


class Mailbox:
    def __init__(self):
        addr, pw = creds()
        self.imap = imaplib.IMAP4_SSL("imap.gmail.com")
        self.imap.login(addr, pw)
        self.folders = {}
        _, boxes = self.imap.list()
        for raw in boxes:
            line = raw.decode()
            name = line.rsplit(' "/" ', 1)[-1].strip().strip('"')
            for flag in ("\\Drafts", "\\All", "\\Sent"):
                if flag in line:
                    self.folders[flag] = name

    def close(self):
        try:
            self.imap.logout()
        except Exception:
            pass

    def draft(self, msg):
        folder = self.folders.get("\\Drafts", "[Gmail]/Drafts")
        self.imap.append(f'"{folder}"', "\\Draft", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
        return msg["Message-ID"]

    def _search(self, query):
        self.imap.select(f'"{self.folders.get(chr(92) + "All", "[Gmail]/All Mail")}"', readonly=True)
        # X-GM-RAW takes one IMAP quoted string: a raw `"` inside it ends the string early (BAD command).
        quoted = query.replace("\\", "\\\\").replace('"', '\\"')
        typ, data = self.imap.search(None, "X-GM-RAW", f'"{quoted}"')
        return data[0].split() if typ == "OK" and data and data[0] else []

    def _header(self, num, name):
        _, data = self.imap.fetch(num, f"(BODY.PEEK[HEADER.FIELDS ({name.upper()})])")
        raw = data[0][1].decode(errors="ignore") if data and data[0] else ""
        m = re.search(rf"{name}:\s*(.+)", raw, re.I)
        return m.group(1).strip() if m else ""

    def sent_to(self, to, since):
        """Message-ID of a message we sent to `to` since YYYY/MM/DD (a draft the user sent)."""
        nums = self._search(f"in:sent to:{to} after:{since}")
        return self._header(nums[0], "Message-ID") if nums else None

    def replied(self, to, since):
        """True if the contact, or anyone else at their company domain, wrote to us since."""
        domain = to.split("@")[1]
        query = f"from:{to} after:{since}" if domain in FREEMAIL else f"from:@{domain} after:{since}"
        return bool(self._search(query + " -in:sent -category:promotions -category:social -category:forums"
                                         " -from:noreply -from:no-reply -from:notifications -from:newsletter"))

    def bounced(self, to, since):
        return bool(self._search(f'{{from:mailer-daemon from:postmaster}} after:{since} "{to}"'))

