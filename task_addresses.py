"""ZIP extraction and display without changing route/stop identity keys."""

import re


def destination_zip(address):
    """Read a US ZIP from the task destination; never infer from route geography."""
    for key in ("postalCode", "zipCode", "postal_code", "zip"):
        value = str(address.get(key) or "").strip()
        if re.fullmatch(r"\d{5}(?:-\d{4})?", value):
            return value
    # Some OnFleet destinations retain ZIP only in the original address.
    match = re.search(r"\b[A-Za-z]{2}[\s,]+(\d{5}(?:-\d{4})?)(?:\s*,?\s*(?:USA|US|United States))?\s*$",
                      str(address.get("unparsed") or ""))
    return match.group(1) if match else ""


def address_with_zip(address, zip_code):
    """Append a known ZIP for display, preserving an existing ZIP/ZIP+4."""
    address = str(address or "").strip()
    zip_code = str(zip_code or "").strip()
    if not address or not re.fullmatch(r"\d{5}(?:-\d{4})?", zip_code):
        return address
    if re.search(r"(?<!\d)" + re.escape(zip_code[:5]) + r"(?:-\d{4})?(?!\d)\s*$", address):
        return address
    return f"{address} {zip_code}"


def task_location_address(address, tasks):
    """Use ZIPs from tasks at this exact stop, keeping grouping keys intact."""
    zip_code = next((t.get("zip") for t in tasks
                     if t.get("full") == address and t.get("zip")), "")
    return address_with_zip(address, zip_code)
