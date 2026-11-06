"""TallyPrime master data, as retrieved (read-only) for ledger mapping in Phase 7.

Fields requested per master are deliberately minimal: what mapping and
validation need, no addresses, no balances, no contact details.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from tally_automation.tally.xmlio import flag, integer, names, text

COMPANY_FIELDS = ("Name", "GUID", "StartingFrom", "BooksFrom", "StateName", "CountryName")
GROUP_FIELDS = ("Name", "GUID", "Parent", "IsRevenue", "IsDeemedPositive", "AffectsGrossProfit", "AlterID")
LEDGER_FIELDS = (
    "Name", "GUID", "Parent", "PartyGSTIN", "GSTRegistrationType", "LedStateName", "CountryName",
    "LedGSTRegDetails", "TaxType", "GSTDutyHead", "RateOfTaxCalculation", "BankDetails", "IFSCode",
    "IsBillWiseOn", "AlterID",
)
VOUCHER_TYPE_FIELDS = ("Name", "GUID", "Parent", "NumberingMethod", "IsActive", "AlterID")

PRIMARY = "Primary"


@dataclass
class Company:
    name: str
    guid: str
    books_from: date | None
    starting_from: date | None
    state: str | None
    country: str | None


@dataclass
class Group:
    guid: str
    name: str
    aliases: list[str]
    parent: str | None            # None for primary groups
    is_revenue: bool | None
    is_deemed_positive: bool | None
    affects_gross_profit: bool | None
    alter_id: int | None
    primary_group: str | None = None


@dataclass
class Ledger:
    guid: str
    name: str
    aliases: list[str]
    parent: str | None
    gstin: str | None
    gst_registration_type: str | None
    state: str | None
    country: str | None
    tax_type: str | None
    gst_duty_head: str | None     # CGST, SGST/UTGST, IGST, Cess for GST duty ledgers
    tax_rate: Decimal | None
    bank_account_number: str | None
    ifsc: str | None
    is_bill_wise: bool | None
    alter_id: int | None
    primary_group: str | None = None


@dataclass
class VoucherType:
    guid: str
    name: str
    aliases: list[str]
    parent: str | None            # the predefined type it is based on, e.g. Purchase
    numbering_method: str | None
    is_active: bool | None
    alter_id: int | None


@dataclass
class Masters:
    groups: list[Group] = field(default_factory=list)
    ledgers: list[Ledger] = field(default_factory=list)
    voucher_types: list[VoucherType] = field(default_factory=list)


def _date(element: ET.Element, tag: str) -> date | None:
    value = text(element, tag)
    if value is None or len(value) != 8 or not value.isdigit():
        return None
    try:
        return date(int(value[:4]), int(value[4:6]), int(value[6:]))
    except ValueError:
        return None


def _decimal(element: ET.Element, tag: str) -> Decimal | None:
    value = text(element, tag)
    try:
        return Decimal(value) if value is not None else None
    except InvalidOperation:
        return None


def _parent(element: ET.Element) -> str | None:
    value = text(element, "PARENT")
    return None if value in (None, PRIMARY) else value


def _items(root: ET.Element, tag: str) -> list[ET.Element]:
    return root.findall(f".//COLLECTION/{tag}")


def parse_companies(root: ET.Element) -> list[Company]:
    companies = []
    for item in _items(root, "COMPANY"):
        name, _ = names(item)
        guid = text(item, "GUID")
        if name and guid:
            companies.append(Company(
                name=name, guid=guid, books_from=_date(item, "BOOKSFROM"),
                starting_from=_date(item, "STARTINGFROM"), state=text(item, "STATENAME"),
                country=text(item, "COUNTRYNAME"),
            ))
    return companies


def parse_groups(root: ET.Element) -> list[Group]:
    groups = []
    for item in _items(root, "GROUP"):
        name, aliases = names(item)
        guid = text(item, "GUID")
        if name and guid:
            groups.append(Group(
                guid=guid, name=name, aliases=aliases, parent=_parent(item),
                is_revenue=flag(item, "ISREVENUE"), is_deemed_positive=flag(item, "ISDEEMEDPOSITIVE"),
                affects_gross_profit=flag(item, "AFFECTSGROSSPROFIT"), alter_id=integer(item, "ALTERID"),
            ))
    return groups


def _latest_gst_registration(item: ET.Element) -> ET.Element | None:
    """TallyPrime 4+ keeps GST registration as dated entries; the latest applies."""
    entries = item.findall("LEDGSTREGDETAILS.LIST")
    if not entries:
        return None
    return max(entries, key=lambda e: text(e, "APPLICABLEFROM") or "")


def parse_ledgers(root: ET.Element) -> list[Ledger]:
    ledgers = []
    for item in _items(root, "LEDGER"):
        name, aliases = names(item)
        guid = text(item, "GUID")
        if not (name and guid):
            continue
        registration = _latest_gst_registration(item)
        gstin = (text(registration, "GSTIN") if registration is not None else None) or text(item, "PARTYGSTIN")
        registration_type = (
            (text(registration, "GSTREGISTRATIONTYPE") if registration is not None else None)
            or text(item, "GSTREGISTRATIONTYPE")
        )
        state = (text(registration, "STATE") if registration is not None else None) or text(item, "LEDSTATENAME")
        ledgers.append(Ledger(
            guid=guid, name=name, aliases=aliases, parent=_parent(item),
            gstin=gstin.upper() if gstin else None, gst_registration_type=registration_type, state=state,
            country=text(item, "COUNTRYNAME"), tax_type=text(item, "TAXTYPE"),
            gst_duty_head=text(item, "GSTDUTYHEAD"), tax_rate=_decimal(item, "RATEOFTAXCALCULATION"),
            bank_account_number=text(item, "BANKDETAILS"), ifsc=text(item, "IFSCODE"),
            is_bill_wise=flag(item, "ISBILLWISEON"), alter_id=integer(item, "ALTERID"),
        ))
    return ledgers


def parse_voucher_types(root: ET.Element) -> list[VoucherType]:
    voucher_types = []
    for item in _items(root, "VOUCHERTYPE"):
        name, aliases = names(item)
        guid = text(item, "GUID")
        if name and guid:
            voucher_types.append(VoucherType(
                guid=guid, name=name, aliases=aliases, parent=_parent(item),
                numbering_method=text(item, "NUMBERINGMETHOD"), is_active=flag(item, "ISACTIVE"),
                alter_id=integer(item, "ALTERID"),
            ))
    return voucher_types


def resolve_primary_groups(masters: Masters) -> list[str]:
    """Fill ``primary_group`` by walking each group's parents. Returns problems found."""
    by_name = {g.name: g for g in masters.groups}
    problems: list[str] = []

    def primary_of(group_name: str | None) -> str | None:
        seen: set[str] = set()
        current = by_name.get(group_name) if group_name else None
        while current is not None and current.parent is not None:
            if current.name in seen:
                problems.append(f"Group hierarchy loops at '{current.name}'.")
                return None
            seen.add(current.name)
            current = by_name.get(current.parent)
        return current.name if current is not None else None

    for group in masters.groups:
        group.primary_group = primary_of(group.name)
    unknown = 0
    for ledger in masters.ledgers:
        ledger.primary_group = primary_of(ledger.parent)
        if ledger.parent and ledger.parent not in by_name:
            unknown += 1
    if unknown:
        problems.append(f"{unknown} ledger(s) name a parent group that was not retrieved.")
    return problems
