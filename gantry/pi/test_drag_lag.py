"""lag_compensated: the magnet leads the dragged piece so the piece rides the path and stops on the centre."""
import math
import unittest

import gantry_driver as gd

LAG = 3.0


def drag(start, magnet_points, lag=LAG, step=0.05):
    """A piece on a 'rope' of length lag behind the magnet: it stays put while the magnet is within lag of it, and is
    pulled straight towards the magnet (keeping the distance lag) once the magnet is further away. Returns the
    piece's track."""
    piece = list(start)
    magnet = list(start)
    track = [tuple(piece)]
    for target in magnet_points:
        dx, dy = target[0] - magnet[0], target[1] - magnet[1]
        n = max(1, int(math.hypot(dx, dy) / step))
        for k in range(1, n + 1):
            mx, my = magnet[0] + dx * k / n, magnet[1] + dy * k / n
            gx, gy = mx - piece[0], my - piece[1]
            d = math.hypot(gx, gy)
            if d > lag:
                piece[0] += gx * (d - lag) / d
                piece[1] += gy * (d - lag) / d
            track.append(tuple(piece))
        magnet = list(target)
    return track


def distance_to_path(point, path):
    best = float("inf")
    for a, b in zip(path, path[1:]):
        ax, ay, bx, by = *a, *b
        vx, vy = bx - ax, by - ay
        t = max(0.0, min(1.0, ((point[0] - ax) * vx + (point[1] - ay) * vy) / (vx * vx + vy * vy)))
        best = min(best, math.hypot(point[0] - ax - t * vx, point[1] - ay - t * vy))
    return best


class LagCompensationTests(unittest.TestCase):
    # e2 -> e4 on the real board as the edge routes plan it: centre, corner, along the e/f line, corner, centre.
    PATH = [(254.0, 102.0), (280.5, 128.5), (280.5, 181.5), (254.0, 208.0)]

    def test_without_it_the_piece_stops_short_and_cuts_corners(self):
        track = drag(self.PATH[0], self.PATH[1:])
        end = track[-1]
        self.assertAlmostEqual(math.dist(end, self.PATH[-1]), LAG, delta=0.1)
        self.assertGreater(max(distance_to_path(p, self.PATH) for p in track), 0.8)

    def test_with_it_the_piece_rides_the_lines_and_stops_on_the_centre(self):
        magnet = gd.lag_compensated(self.PATH, LAG)
        track = drag(self.PATH[0], magnet)
        self.assertLess(math.dist(track[-1], self.PATH[-1]), 0.05)
        self.assertLess(max(distance_to_path(p, self.PATH) for p in track), 0.05)
        # It passes every corner of the path exactly.
        for corner in self.PATH[1:-1]:
            self.assertLess(min(math.dist(p, corner) for p in track), 0.05)

    def test_the_waypoints(self):
        r = 1 / math.sqrt(2)
        got = gd.lag_compensated(self.PATH, LAG)
        want = [(280.5 + LAG * r, 128.5 + LAG * r), (280.5, 128.5 + LAG),        # past the corner, then swing up
                (280.5, 181.5 + LAG), (280.5 - LAG * r, 181.5 + LAG * r),        # past the next, then swing in
                (254.0 - LAG * r, 208.0 + LAG * r)]                             # past e4's centre on the way in
        self.assertEqual(len(got), len(want))
        for g, w in zip(got, want):
            self.assertAlmostEqual(g[0], w[0], places=2)
            self.assertAlmostEqual(g[1], w[1], places=2)

    def test_zero_lag_is_the_plain_path_and_travel_clamps(self):
        self.assertEqual(gd.lag_compensated(self.PATH, 0.0), self.PATH[1:])
        clamped = gd.lag_compensated([(42.0, 367.0), (42.0, 420.0)], LAG, travel=(470.0, 420.0))
        self.assertEqual(clamped, [(42.0, 420.0)])            # rank 8 sits on the Y limit: no room to lead there

    def test_the_carry_gcode_uses_it(self):
        config = gd.load_config(gd.HERE / "gantry_config.json")
        config["drag_lag_mm"] = LAG
        lines = gd.plan(gd.simple_move_job("e2e4", occupied={"e2"}), gd.BoardMap(config), config)
        carry = [text for text, note in lines if note.startswith("carry ")]
        self.assertEqual(len(carry), 5)
        self.assertTrue(carry[-1].startswith(f"G1 X{254.0 - LAG / math.sqrt(2):.3f} Y{208.0 + LAG / math.sqrt(2):.3f}"))


if __name__ == "__main__":
    unittest.main()
