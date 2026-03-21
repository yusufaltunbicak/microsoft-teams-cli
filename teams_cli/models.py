from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone

try:
    from bs4 import MarkupResemblesLocatorWarning
    warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)
except ImportError:
    pass


@dataclass
class User:
    id: str
    display_name: str
    email: str
    user_type: str = ""  # "orgid", "guest"

    @classmethod
    def from_api(cls, data: dict) -> User:
        # Handle various API response formats
        mri = data.get("mri", data.get("id", ""))
        # Extract orgid from MRI format: 8:orgid:{guid}
        user_id = mri
        if "8:orgid:" in mri:
            user_id = mri.split("8:orgid:")[-1]

        return cls(
            id=user_id,
            display_name=data.get("displayName", data.get("display_name", "")),
            email=data.get("email", data.get("mail", data.get("userPrincipalName", ""))),
            user_type=data.get("userType", data.get("type", "")),
        )

    @classmethod
    def from_profile(cls, data: dict) -> User:
        """Parse from MT fetchShortProfile response."""
        return cls(
            id=data.get("mri", "").replace("8:orgid:", ""),
            display_name=data.get("displayName", ""),
            email=data.get("email", data.get("userPrincipalName", "")),
            user_type=data.get("type", ""),
        )

    def __str__(self) -> str:
        if self.email:
            return f"{self.display_name} <{self.email}>"
        return self.display_name


@dataclass
class Chat:
    id: str
    topic: str
    chat_type: str  # oneOnOne, group, meeting
    last_message_preview: str
    last_message_time: datetime
    last_message_sender: str
    members: list[str] = field(default_factory=list)
    unread_count: int = 0
    display_num: int = 0

    @classmethod
    def from_api(cls, data: dict) -> Chat:
        # IC3 chat service format
        thread_props = data.get("threadProperties", {})
        last_msg = data.get("lastMessage", {})

        # Get topic or member names for 1:1
        topic = thread_props.get("topic", "")
        chat_type = _detect_chat_type(data.get("id", ""), thread_props)

        # Members from threadProperties
        members = []
        member_list = thread_props.get("members", [])
        if isinstance(member_list, str):
            import json
            try:
                member_list = json.loads(member_list)
            except (json.JSONDecodeError, TypeError):
                member_list = []
        if isinstance(member_list, list):
            for m in member_list:
                if isinstance(m, dict):
                    members.append(m.get("friendlyName", m.get("id", "")))
                elif isinstance(m, str):
                    members.append(m)

        # Last message info
        last_content = last_msg.get("content", last_msg.get("preview", ""))
        last_sender = last_msg.get("imdisplayname", last_msg.get("from", ""))
        last_time = _parse_dt(
            last_msg.get("composetime",
            last_msg.get("originalarrivaltime", ""))
        )

        return cls(
            id=data.get("id", ""),
            topic=topic,
            chat_type=chat_type,
            last_message_preview=_strip_html(last_content)[:100],
            last_message_time=last_time,
            last_message_sender=last_sender,
            members=members,
            unread_count=_parse_unread_count(thread_props),
        )

    @classmethod
    def from_graph(cls, data: dict) -> Chat:
        """Parse from Graph API /me/chats format."""
        topic = data.get("topic", "")
        chat_type = data.get("chatType", "oneOnOne")
        last_msg = data.get("lastMessagePreview", {})

        return cls(
            id=data.get("id", ""),
            topic=topic or "",
            chat_type=chat_type,
            last_message_preview=last_msg.get("body", {}).get("content", "")[:100] if last_msg else "",
            last_message_time=_parse_dt(last_msg.get("createdDateTime", "")) if last_msg else datetime.min.replace(tzinfo=timezone.utc),
            last_message_sender=last_msg.get("from", {}).get("user", {}).get("displayName", "") if last_msg else "",
        )

    @property
    def display_title(self) -> str:
        """Return a display-friendly title."""
        if self.topic:
            return self.topic
        if self.members:
            return ", ".join(self.members[:3])
        # Fallback: extract from conversation ID
        conv_id = self.id
        if "@unq.gbl.spaces" in conv_id:
            return "1:1 Chat"
        if "meeting_" in conv_id:
            return "Meeting Chat"
        return "Group Chat"


@dataclass
class Message:
    id: str
    conversation_id: str
    sender: str
    sender_id: str
    content: str
    message_type: str  # Text, RichText/Html
    timestamp: datetime
    client_message_id: str = ""
    importance: str = ""
    subject: str = ""
    is_from_me: bool = False
    text_content: str = ""
    reactions: list[Reaction] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    display_num: int = 0
    chat_title: str = ""

    @classmethod
    def from_api(cls, data: dict, my_user_id: str = "") -> Message:
        sender = data.get("imdisplayname", "")
        sender_id = data.get("from", "")
        # Clean sender_id: "8:orgid:{guid}" → "{guid}"
        clean_sender_id = sender_id
        if "8:orgid:" in sender_id:
            clean_sender_id = sender_id.split("8:orgid:")[-1]

        content = data.get("content", "")
        msg_type = data.get("messagetype", "")

        # Skip system messages
        if msg_type in ("ThreadActivity/AddMember", "ThreadActivity/MemberJoined",
                        "ThreadActivity/MemberLeft", "ThreadActivity/TopicUpdate",
                        "ThreadActivity/DeleteMember", "Event/Call",
                        "ThreadActivity/TabUpdated"):
            content = f"[{msg_type}]"

        # Reactions
        reactions = []
        for key, value in data.get("properties", {}).items():
            if key == "emotions":
                import json
                try:
                    emotions = json.loads(value) if isinstance(value, str) else value
                    for emotion in emotions:
                        for user_entry in emotion.get("users", []):
                            reactions.append(Reaction(
                                emoji=emotion.get("key", ""),
                                user=user_entry.get("value", ""),
                                user_id=user_entry.get("mri", ""),
                            ))
                except (json.JSONDecodeError, TypeError):
                    pass

        # Attachments: from properties.files + inline images in HTML
        attachments = []
        files_prop = data.get("properties", {}).get("files", "[]")
        if isinstance(files_prop, str):
            import json as _json
            try:
                files_prop = _json.loads(files_prop)
            except (ValueError, TypeError):
                files_prop = []
        if isinstance(files_prop, list):
            for att_data in files_prop:
                if isinstance(att_data, dict):
                    attachments.append(Attachment.from_api(att_data))

        # Extract inline images from HTML content
        attachments.extend(_extract_inline_images(content))

        is_from_me = (clean_sender_id == my_user_id) if my_user_id else False

        return cls(
            id=data.get("id", data.get("sequenceId", data.get("version", ""))),
            conversation_id=data.get("conversationid", data.get("conversationId", "")),
            sender=sender,
            sender_id=clean_sender_id,
            content=content,
            message_type=msg_type,
            timestamp=_parse_dt(data.get("composetime", data.get("originalarrivaltime", ""))),
            client_message_id=data.get("clientmessageid", ""),
            importance=data.get("properties", {}).get("importance", ""),
            subject=data.get("properties", {}).get("subject", ""),
            is_from_me=is_from_me,
            text_content=_strip_html(content),
            reactions=reactions,
            attachments=attachments,
        )


@dataclass
class Reaction:
    emoji: str  # like, heart, laugh, surprised, sad, angry
    user: str
    user_id: str = ""


@dataclass
class Attachment:
    id: str
    name: str
    content_type: str
    content_url: str
    size: int = 0
    is_inline: bool = False

    @classmethod
    def from_api(cls, data: dict) -> Attachment:
        # URL can be in contentUrl, objectUrl, or nested in fileInfo.fileUrl
        url = data.get("contentUrl", data.get("objectUrl", ""))
        if not url:
            file_info = data.get("fileInfo", {})
            if isinstance(file_info, dict):
                url = file_info.get("fileUrl", "")
        return cls(
            id=data.get("id", data.get("@id", data.get("itemid", ""))),
            name=data.get("title", data.get("name", data.get("fileName", ""))),
            content_type=data.get("contentType", data.get("fileType", "")),
            content_url=url,
            size=int(data.get("fileSize", data.get("size", 0)) or 0),
        )

    @classmethod
    def from_inline_image(cls, src: str, img_type: str = "png", index: int = 0) -> Attachment:
        # Extract object ID from ASM URL
        obj_id = ""
        if "/objects/" in src:
            obj_id = src.split("/objects/")[1].split("/")[0]
        ext = img_type if img_type else "png"
        return cls(
            id=obj_id,
            name=f"image_{index + 1}.{ext}",
            content_type=f"image/{ext}",
            content_url=src,
            is_inline=True,
        )


def _parse_unread_count(thread_props: dict) -> int:
    """Extract unread count from thread properties, handling both dict and string formats."""
    ch = thread_props.get("consumptionhorizon")
    if isinstance(ch, dict):
        try:
            return int(ch.get("unreadCount", 0))
        except (ValueError, TypeError):
            pass
    # Also check direct unread count field
    try:
        count = int(thread_props.get("unreadMessageCount", 0))
        if count:
            return count
    except (ValueError, TypeError):
        pass
    return 0


def _extract_inline_images(content: str) -> list[Attachment]:
    """Extract inline image attachments from HTML message content."""
    if not content or "<img" not in content:
        return []
    import re
    images = []
    # Match <img> tags with AMSImage or asm.skype.com URLs
    for i, match in enumerate(re.finditer(
        r'<img[^>]+src="(https://[^"]*asm\.skype\.com[^"]*)"[^>]*(?:itemscope="([^"]*)")?',
        content,
    )):
        src = match.group(1)
        img_type = match.group(2) or "png"
        images.append(Attachment.from_inline_image(src, img_type, index=i))
    return images


def _detect_chat_type(conv_id: str, thread_props: dict) -> str:
    """Detect chat type from conversation ID and properties."""
    thread_type = thread_props.get("threadType", "")
    if thread_type:
        return thread_type  # "chat", "meeting", etc.
    if "@unq.gbl.spaces" in conv_id:
        return "oneOnOne"
    if "meeting_" in conv_id:
        return "meeting"
    if "@thread" in conv_id:
        return "group"
    return "unknown"


def _parse_dt(s: str) -> datetime:
    from datetime import timezone as tz
    if not s:
        return datetime.min.replace(tzinfo=tz.utc)
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz.utc)
        return dt
    except ValueError:
        return datetime.min.replace(tzinfo=tz.utc)


def _strip_html(html: str) -> str:
    """Strip HTML tags for plain text display."""
    if not html:
        return ""
    try:
        from bs4 import BeautifulSoup
        return BeautifulSoup(html, "html.parser").get_text(strip=True)
    except ImportError:
        import re
        return re.sub(r"<[^>]+>", "", html)
