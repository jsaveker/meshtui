"""Compact terminal QR rendering shared by MeshTUI views."""

from __future__ import annotations

from rich.text import Text


def qr_text(payload: str) -> Text | None:
    """Render *payload* as a scannable half-block QR code.

    The absolute white-on-black style keeps the code readable under every
    MeshTUI theme. ``None`` is retained as a graceful fallback for source
    checkouts where the optional runtime environment is incomplete.
    """
    try:
        import qrcode
    except ImportError:
        return None

    qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(payload)
    qr.make(fit=True)
    matrix = qr.get_matrix()  # True = dark module; border rows included
    blocks = {
        (True, True): " ",
        (True, False): "▄",
        (False, True): "▀",
        (False, False): "█",
    }
    out = Text()
    for row in range(0, len(matrix), 2):
        top = matrix[row]
        bottom = matrix[row + 1] if row + 1 < len(matrix) else [False] * len(top)
        out.append("".join(blocks[(upper, lower)]
                           for upper, lower in zip(top, bottom)) + "\n",
                   style="white on black")
    return out
