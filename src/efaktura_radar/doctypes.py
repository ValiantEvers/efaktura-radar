"""Dokumenttype-identifikatorer for EHF / Peppol BIS Billing 3.0.

Norge har **ingen egen** dokumenttype for fakturering. EHF Fakturering *er*
Peppol BIS Billing 3.0 — DFØs spesifikasjon heter «EHF Fakturering og Peppol
BIS Billing», og Peppol-kodelisten (v9.7, publisert 2026-07-02) inneholder
ingen Norge-spesifikk billing-doctype.

Fella å unngå: to av typene deler UBL-rot (Order Response / Order Agreement,
Catalogue / Punch Out). Match alltid på hele identifikatorstrengen, aldri på
rotelementet.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

__all__ = [
    "CREDIT_NOTE",
    "DOCTYPE_SCHEME",
    "INVOICE",
    "INVOICE_CII",
    "INVOICE_TYPES",
    "OTHER_POST_AWARD",
    "PROCESS_BILLING",
    "supports_ehf_invoice",
]

DOCTYPE_SCHEME: Final = "busdox-docid-qns"

_BILLING_CUSTOMIZATION: Final = (
    "urn:cen.eu:en16931:2017#compliant#urn:fdc:peppol.eu:2017:poacc:billing:3.0"
)

#: Faktura (UBL). Dette er strengen som avgjør «kan motta EHF-faktura».
INVOICE: Final = (
    "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2::Invoice"
    f"##{_BILLING_CUSTOMIZATION}::2.1"
)

#: Kreditnota (UBL). Nesten alltid registrert sammen med faktura.
CREDIT_NOTE: Final = (
    "urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2::CreditNote"
    f"##{_BILLING_CUSTOMIZATION}::2.1"
)

#: CII-varianten. Sjelden i Norge, men gyldig — godta den.
INVOICE_CII: Final = (
    "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100::CrossIndustryInvoice"
    f"##{_BILLING_CUSTOMIZATION}::D16B"
)

#: Prosessidentifikator for både faktura og kreditnota.
PROCESS_BILLING: Final = "urn:fdc:peppol.eu:2017:poacc:billing:01:1.0"

#: Alt som teller som «kan motta en faktura».
INVOICE_TYPES: Final = frozenset({INVOICE, INVOICE_CII})

#: Øvrige post-award-typer. Tas med fordi et regnskapsbyrå vil se hele bildet —
#: at en kunde støtter ordre er ikke det samme som at den støtter faktura.
OTHER_POST_AWARD: Final = {
    "order": "urn:oasis:names:specification:ubl:schema:xsd:Order-2::Order"
    "##urn:fdc:peppol.eu:poacc:trns:order:3::2.1",
    "order_response": "urn:oasis:names:specification:ubl:schema:xsd:"
    "OrderResponse-2::OrderResponse"
    "##urn:fdc:peppol.eu:poacc:trns:order_response:3::2.1",
    "order_agreement": "urn:oasis:names:specification:ubl:schema:xsd:"
    "OrderResponse-2::OrderResponse"
    "##urn:fdc:peppol.eu:poacc:trns:order_agreement:3::2.1",
    "despatch_advice": "urn:oasis:names:specification:ubl:schema:xsd:"
    "DespatchAdvice-2::DespatchAdvice"
    "##urn:fdc:peppol.eu:poacc:trns:despatch_advice:3::2.1",
    "catalogue": "urn:oasis:names:specification:ubl:schema:xsd:Catalogue-2::Catalogue"
    "##urn:fdc:peppol.eu:poacc:trns:catalogue:3::2.1",
    "punch_out": "urn:oasis:names:specification:ubl:schema:xsd:Catalogue-2::Catalogue"
    "##urn:fdc:peppol.eu:poacc:trns:punch_out:3::2.1",
    "invoice_response": "urn:oasis:names:specification:ubl:schema:xsd:"
    "ApplicationResponse-2::ApplicationResponse"
    "##urn:fdc:peppol.eu:poacc:trns:invoice_response:3::2.1",
}


def supports_ehf_invoice(doctypes: Iterable[str]) -> bool:
    """Finnes en EHF-fakturatype blant de registrerte dokumenttypene?

    Kreditnota alene teller
    ikke — en mottaker kan i prinsippet ha registrert kreditnota uten faktura,
    og da kan du ikke sende faktura dit.
    """
    return any(d in INVOICE_TYPES for d in doctypes)
