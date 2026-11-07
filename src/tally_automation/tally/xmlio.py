"""TallyPrime XML: building read-only export requests and parsing responses.

Only one kind of request exists here: ``Export`` of a ``Collection`` defined
inline with TDL (the request's own scope; nothing is stored in Tally). Every
request body passes ``assert_read_only`` before it can be sent.

Observed behaviour of TallyPrime 6.2 that parsing must handle:

* Values contain XML-illegal control characters, both raw (e.g. ``\\x02``) and as
  character references (``&#4;`` marks Tally's "not applicable"). They are
  removed before parsing and counted.
* An unknown company name is *not* an error: Tally answers ``STATUS 1`` with no
  records. Callers must check the company against the loaded list and treat
  an empty result as a failure where data must exist.
* Errors come back as ``<LINEERROR>``, as a bare ``<RESPONSE>`` text, as
  ``<STATUS>0</STATUS>`` / ``-1``, or as an empty body.
* Vouchers with ledger entries contain tags with an undeclared ``UDF:``
  namespace prefix (user-defined fields), which strict parsers reject. Prefixes
  are rewritten to plain tag names (``UDF:X`` → ``UDF.X``) before parsing.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from xml.sax.saxutils import escape

_CONTROL_REFS = re.compile(r"&#(?:x0*[0-8bBcCeEfF]|x0*1[0-9a-fA-F]|0*[0-8]|0*1[124-9]|0*2\d|0*3[01]);")
_CONTROL_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")
_COLLECTION_ID = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,40}$")
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,40}$")
_TAG = re.compile(r"<[^<>]+>")
_PREFIXED_TAG = re.compile(r"(</?)([A-Za-z_][\w.-]*):(?=[A-Za-z_])")
_PREFIXED_ATTR = re.compile(r"(\s)(?!xmlns:)([A-Za-z_][\w.-]*):([A-Za-z_][\w.-]*\s*=)")

# Words that must never appear in a request body. Tally imports and alters data
# through ``<TALLYREQUEST>Import</TALLYREQUEST>``, ``<IMPORTDATA>`` and
# ``ACTION="Create|Alter|Delete"``; none of those can be built by this module,
# and this list makes sure no future change can send them either.
_FORBIDDEN = re.compile(
    r"<\s*IMPORTDATA|<\s*REQUESTDATA|<\s*TALLYMESSAGE|\bACTION\s*=|Import\s*</\s*TALLYREQUEST|"
    r"<\s*TALLYREQUEST\s*>\s*(?!Export\s*<)",
    re.IGNORECASE,
)


class TallyWriteBlocked(RuntimeError):
    """A request that is not a plain export was about to be sent."""


class TallyResponseError(Exception):
    """Tally answered, but not with usable data."""


def assert_read_only(body: str) -> None:
    if body.count("<TALLYREQUEST>") != 1 or "<TALLYREQUEST>Export</TALLYREQUEST>" not in body:
        raise TallyWriteBlocked("Only Export requests may be sent to TallyPrime.")
    if _FORBIDDEN.search(body):
        raise TallyWriteBlocked("The request contains an import/alter instruction and was not sent.")


def export_collection_request(collection_type: str, fields: tuple[str, ...], company: str | None) -> str:
    """An Export request for one inline TDL collection of masters.

    ``collection_type`` and ``fields`` come from code (fixed lists), never from
    users; they are still checked. ``company`` is escaped.
    """
    if not _NAME.match(collection_type) or not all(_NAME.match(f) for f in fields):
        raise ValueError("invalid collection type or field name")
    collection_id = f"App{collection_type}"
    assert _COLLECTION_ID.match(collection_id)
    variables = "<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"
    if company is not None:
        variables += f"<SVCURRENTCOMPANY>{escape(company)}</SVCURRENTCOMPANY>"
    methods = "".join(f"<NATIVEMETHOD>{f}</NATIVEMETHOD>" for f in fields)
    body = (
        "<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE>"
        f"<ID>{collection_id}</ID></HEADER><BODY><DESC><STATICVARIABLES>{variables}</STATICVARIABLES>"
        f'<TDL><TDLMESSAGE><COLLECTION NAME="{collection_id}" ISINITIALIZE="Yes"><TYPE>{collection_type}</TYPE>'
        f"{methods}</COLLECTION></TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"
    )
    assert_read_only(body)
    return body


@dataclass
class ParsedResponse:
    root: ET.Element
    removed_characters: int   # XML-illegal control characters dropped before parsing


def decode(content: bytes, content_type: str | None) -> str:
    if content.startswith((b"\xff\xfe", b"\xfe\xff")):
        return content.decode("utf-16")
    match = re.search(r"charset=([\w-]+)", content_type or "", re.IGNORECASE)
    encoding = match.group(1) if match else "utf-8"
    try:
        return content.decode(encoding)
    except (LookupError, UnicodeDecodeError) as exc:
        raise TallyResponseError(f"TallyPrime sent text that is not valid {encoding}.") from exc


def parse_response(text: str) -> ParsedResponse:
    if not text.strip():
        raise TallyResponseError("TallyPrime returned an empty response (unknown request or report).")
    # No DTDs: entity declarations are the classic XML attack and Tally never sends them.
    if re.search(r"<!(DOCTYPE|ENTITY)", text, re.IGNORECASE):
        raise TallyResponseError("TallyPrime's response contained a DTD and was refused.")
    cleaned, refs = _CONTROL_REFS.subn("", text)
    cleaned, chars = _CONTROL_CHARS.subn("", cleaned)
    cleaned = unprefix(cleaned)
    try:
        root = ET.fromstring(cleaned)
    except ET.ParseError as exc:
        raise TallyResponseError(f"TallyPrime's response is not well-formed XML ({exc}).") from exc

    if root.tag == "RESPONSE":
        raise TallyResponseError(f"TallyPrime: {(root.text or '').strip()[:300] or 'error response'}")
    errors = [(e.text or "").strip() for e in root.iter("LINEERROR") if (e.text or "").strip()]
    if errors:
        raise TallyResponseError("TallyPrime: " + "; ".join(errors)[:500])
    status = (root.findtext("HEADER/STATUS") or "").strip()
    if status and status != "1":
        raise TallyResponseError(f"TallyPrime reported status {status} without data.")
    return ParsedResponse(root, refs + chars)


def unprefix(text: str) -> str:
    """Rewrite namespace prefixes on tags and attributes (``UDF:X`` → ``UDF.X``); xmlns declarations stay."""
    # Attributes are rewritten only inside tags, never in text (a narration may contain "ref:abc=").
    def fix(match: re.Match[str]) -> str:
        return _PREFIXED_ATTR.sub(r"\1\2.\3", _PREFIXED_TAG.sub(r"\1\2.", match.group()))

    return _TAG.sub(fix, text)


# ---- Value helpers ---------------------------------------------------------------------

def text(element: ET.Element, tag: str) -> str | None:
    value = (element.findtext(tag) or "").strip()
    return value or None


def flag(element: ET.Element, tag: str) -> bool | None:
    value = text(element, tag)
    if value is None:
        return None
    return value.lower() == "yes"


def integer(element: ET.Element, tag: str) -> int | None:
    value = text(element, tag)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def names(element: ET.Element) -> tuple[str | None, list[str]]:
    """The master's name (``NAME`` attribute) and its aliases (LANGUAGENAME.LIST)."""
    name = (element.get("NAME") or "").strip() or text(element, "NAME")
    listed = [
        (n.text or "").strip() for n in element.findall("LANGUAGENAME.LIST/NAME.LIST/NAME")
    ] or [(n.text or "").strip() for n in element.findall("LANGUAGENAME.LIST/NAME")]
    aliases = [a for a in dict.fromkeys(listed) if a and a != name]
    return name, aliases
