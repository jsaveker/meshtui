"""A braille-dot drawing surface.

Each terminal cell holds a 2x4 grid of braille dots (U+2800 + bitmask), so a
90x30 pane becomes a 180x120 pixel canvas. Braille is unambiguously
narrow-width in every terminal, unlike most geometric symbols, so it stays
aligned everywhere.

A separate text layer sits on top for labels, which take precedence over dots.
"""

from __future__ import annotations

import math

from rich.text import Text

BRAILLE_BASE = 0x2800
# Dot bit for [dy][dx] within a cell. Braille numbers dots 1-8 in a famously
# non-obvious order; this table is that order flattened.
DOT_BITS = (
    (0x01, 0x08),
    (0x02, 0x10),
    (0x04, 0x20),
    (0x40, 0x80),
)


class BrailleCanvas:
    def __init__(self, cols: int, rows: int) -> None:
        self.cols = max(1, cols)
        self.rows = max(1, rows)
        self.width = self.cols * 2
        self.height = self.rows * 4
        self._mask = bytearray(self.cols * self.rows)
        self._style: list[str | None] = [None] * (self.cols * self.rows)
        self._text: dict[tuple[int, int], tuple[str, str]] = {}

    # ------------------------------------------------------------ drawing

    def plot(self, x: float, y: float, style: str = "white") -> None:
        xi, yi = int(x), int(y)
        if not (0 <= xi < self.width and 0 <= yi < self.height):
            return
        idx = (yi // 4) * self.cols + (xi // 2)
        self._mask[idx] |= DOT_BITS[yi % 4][xi % 2]
        self._style[idx] = style

    def blob(self, x: float, y: float, size: int = 2, style: str = "white") -> None:
        """A small filled square of dots, for marks that must stand out."""
        xi, yi = int(x), int(y)
        for dy in range(size):
            for dx in range(size):
                self.plot(xi + dx - size // 2, yi + dy - size // 2, style)

    def line(self, x0: float, y0: float, x1: float, y1: float, style: str = "white") -> None:
        x0, y0, x1, y1 = int(x0), int(y0), int(x1), int(y1)
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx + dy
        while True:
            self.plot(x0, y0, style)
            if x0 == x1 and y0 == y1:
                return
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy

    def dashed_circle(self, cx: float, cy: float, r: float, style: str = "grey35",
                      on: int = 2, off: int = 3) -> None:
        if r <= 0:
            return
        steps = max(24, int(2 * math.pi * r))
        for i in range(steps):
            if i % (on + off) >= on:
                continue
            theta = 2 * math.pi * i / steps
            self.plot(cx + r * math.cos(theta), cy + r * math.sin(theta), style)

    def label(self, col: int, row: int, text: str, style: str = "white") -> None:
        for i, ch in enumerate(text):
            if 0 <= row < self.rows and 0 <= col + i < self.cols:
                self._text[(row, col + i)] = (ch, style)

    def label_fits(self, col: int, row: int, length: int) -> bool:
        if not (0 <= row < self.rows):
            return False
        if col < 0 or col + length > self.cols:
            return False
        return all((row, col + i) not in self._text for i in range(length))

    # ----------------------------------------------------------- rendering

    def render(self) -> Text:
        out = Text(no_wrap=True, overflow="crop")
        for r in range(self.rows):
            if r:
                out.append("\n")
            base = r * self.cols
            for c in range(self.cols):
                cell = self._text.get((r, c))
                if cell is not None:
                    out.append(cell[0], style=cell[1])
                    continue
                mask = self._mask[base + c]
                if mask:
                    out.append(chr(BRAILLE_BASE + mask), style=self._style[base + c] or "white")
                else:
                    out.append(" ")
        return out
