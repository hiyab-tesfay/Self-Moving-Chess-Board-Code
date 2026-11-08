# Calibration token (ArUco id 10)

The camera calibration finds this token on the board and measures where the
magnet put it. The marker is DICT_4X4_50 **id 10** (ids 0-3 are the board's
corner markers), with a **20 mm** black square.

## Print

1. Print `token_id10.svg` (or `token_id10.png`, 762 dpi) at **100% / actual size**:
   turn off "fit to page" / "scale to fit".
2. Measure the bar at the bottom: it must be **50 mm**. Each black square must
   be **20 mm**. If not, fix the printer scaling and print again.
   (The camera code checks the marker's size and lowers its confidence if it
   is not about 20 mm.)
3. Matte paper or a matte sticker sheet is best; glossy paper glares under the lamp.

The sheet has four tokens; you need one. The others are spares.

## Make the token

- Cut one marker out along its dashed **30 mm** circle. Keep the white ring around
  the black square: the camera needs it to see the square's edge.
- Use a flat, rigid disc of **30-32 mm** diameter: a draughts/checker piece, a
  coin, or a bottle cap (open side down). A smaller disc cannot hold the marker:
  the 20 mm square's corners reach 14.1 mm from its centre.
- Glue the paper **centred** on the disc, flat, no bubbles. The camera measures the
  centre of the black square, so an off-centre sticker becomes a calibration error
  of the same size. Line it up by eye against the dashed circle, which is concentric
  with the square.
- Under the disc fix a thin steel washer or the same magnet a chess piece uses, also
  **centred**, so the gantry magnet pulls the token's centre over itself.
- Keep the token low (a few mm): the code ignores camera parallax for a flat token.

## Use

- Put exactly **one** token on the board, face up. Keep the spare sheet, hands and
  the gantry carriage out of the camera view; two markers with id 10 is an error.
- Regenerate the files with `python make_token.py` (OpenCV with aruco, NumPy,
  Pillow for the PNG's dpi).
