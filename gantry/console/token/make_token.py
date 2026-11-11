"""Make the printable calibration token: ArUco DICT_4X4_50 id 10, 20 mm black square.

Writes token_id10.svg (true-scale, sized in mm) and token_id10.png (762 dpi =
30 px/mm, dpi stored in the file) next to this script. The sheet holds four
tokens (spares) inside 30 mm dashed cut circles, plus a 50 mm scale bar to
check the print scale.

Run: python make_token.py   (needs opencv-contrib with aruco, numpy, Pillow for the PNG dpi)
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

TOKEN_ID = 10
MARKER_MM = 20.0          # black square side
CUT_MM = 30.0             # cut circle diameter (white margin around the marker)
SHEET_MM = (90.0, 100.0)  # width, height
PX_PER_MM = 30            # PNG resolution: 30 px/mm = 762 dpi
CENTRES = [(22.5, 22.5), (67.5, 22.5), (22.5, 62.5), (67.5, 62.5)]
SCALE_BAR = (20.0, 88.0, 50.0)   # x, y, length in mm
HERE = Path(__file__).resolve().parent


def marker_bits() -> np.ndarray:
    """6x6 cells (border included), 1 = black."""
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    image = cv2.aruco.generateImageMarker(dictionary, TOKEN_ID, 6)
    return (image < 128).astype(np.uint8)


def svg(bits: np.ndarray) -> str:
    w, h = SHEET_MM
    cell = MARKER_MM / bits.shape[0]
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}mm" height="{h}mm" viewBox="0 0 {w} {h}">',
           f'<rect width="{w}" height="{h}" fill="white"/>']
    for cx, cy in CENTRES:
        out.append(f'<circle cx="{cx}" cy="{cy}" r="{CUT_MM / 2}" fill="white" stroke="#999" '
                   f'stroke-width="0.2" stroke-dasharray="1 1"/>')
        x0, y0 = cx - MARKER_MM / 2, cy - MARKER_MM / 2
        for r, c in zip(*np.nonzero(bits)):
            out.append(f'<rect x="{x0 + c * cell:.4f}" y="{y0 + r * cell:.4f}" width="{cell + 0.01:.4f}" '
                       f'height="{cell + 0.01:.4f}" fill="black"/>')
        out.append(f'<text x="{cx}" y="{cy + CUT_MM / 2 + 3}" font-family="sans-serif" font-size="2.2" '
                   f'text-anchor="middle" fill="#666">ArUco 4x4 id {TOKEN_ID} - 20 mm</text>')
    x, y, length = SCALE_BAR
    out.append(f'<rect x="{x}" y="{y}" width="{length}" height="1.5" fill="black"/>')
    for k in range(6):
        out.append(f'<rect x="{x + k * 10 - 0.15}" y="{y - 1.5}" width="0.3" height="3" fill="black"/>')
    out.append(f'<text x="{x + length / 2}" y="{y + 5.5}" font-family="sans-serif" font-size="2.6" '
               f'text-anchor="middle">this bar must measure 50 mm - print at 100% / actual size</text>')
    out.append('</svg>')
    return "\n".join(out) + "\n"


def png(bits: np.ndarray) -> np.ndarray:
    k = PX_PER_MM
    w, h = SHEET_MM
    sheet = np.full((int(h * k), int(w * k)), 255, np.uint8)
    cell = int(round(MARKER_MM / bits.shape[0] * k))   # 100 px
    assert cell * bits.shape[0] == MARKER_MM * k, "marker must be a whole number of pixels per cell"
    big = np.kron(bits, np.ones((cell, cell), np.uint8))
    for cx, cy in CENTRES:
        x0, y0 = int(round((cx - MARKER_MM / 2) * k)), int(round((cy - MARKER_MM / 2) * k))
        region = sheet[y0:y0 + big.shape[0], x0:x0 + big.shape[1]]
        region[big == 1] = 0
        centre = (int(cx * k), int(cy * k))
        for a in range(0, 360, 12):   # dashed cut circle
            cv2.ellipse(sheet, centre, (int(CUT_MM / 2 * k),) * 2, 0, a, a + 6, 150, 4, cv2.LINE_AA)
        cv2.putText(sheet, f"ArUco 4x4 id {TOKEN_ID} - 20 mm", (int((cx - 13) * k), int((cy + CUT_MM / 2 + 3) * k)),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.6, 100, 3, cv2.LINE_AA)
    x, y, length = SCALE_BAR
    sheet[int(y * k):int((y + 1.5) * k), int(x * k):int((x + length) * k)] = 0
    for i in range(6):
        xi = int((x + i * 10) * k)
        sheet[int((y - 1.5) * k):int((y + 1.5) * k), xi - 4:xi + 5] = 0
    cv2.putText(sheet, "this bar must measure 50 mm - print at 100% / actual size",
                (int((x - 12) * k), int((y + 6) * k)), cv2.FONT_HERSHEY_SIMPLEX, 1.7, 0, 3, cv2.LINE_AA)
    return sheet


def main() -> None:
    bits = marker_bits()
    (HERE / "token_id10.svg").write_text(svg(bits))
    sheet = png(bits)
    dpi = PX_PER_MM * 25.4
    try:
        from PIL import Image
        Image.fromarray(sheet).save(HERE / "token_id10.png", dpi=(dpi, dpi))
    except ImportError:   # no Pillow: still write the PNG, print at 762 dpi by hand
        cv2.imwrite(str(HERE / "token_id10.png"), sheet)
    # Check the PNG decodes as id 10 with the expected size.
    detector = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50),
                                       cv2.aruco.DetectorParameters())
    corners, ids, _ = detector.detectMarkers(sheet)
    found = [] if ids is None else ids.flatten().tolist()
    assert found.count(TOKEN_ID) == len(CENTRES) and len(found) == len(CENTRES), found
    side = np.mean([np.linalg.norm(c[0][0] - c[0][1]) for c in corners]) / PX_PER_MM
    print(f"wrote token_id10.svg and token_id10.png ({dpi:.0f} dpi); {len(found)} markers id {TOKEN_ID}, "
          f"side {side:.2f} mm")


if __name__ == "__main__":
    main()
