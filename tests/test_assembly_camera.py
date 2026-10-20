"""Camera quality for the Assembly guide drawings: reuse, the scored fallback, turning a model over for an upward join, and
the close-up frame.

The seam is assembly_render's camera choice (choose_view / choose_camera) and the step art prepare_step makes from it. Scenes
are small boxes written straight into a Scene, as the guide would see a split. Tests assert on external facts (which steps
turn, which steps get a close-up, which side the camera is on, determinism), never on pixels."""
import io
import unittest
from unittest import mock

from tests.test_assembly_plan import GEOMETRY, part, joint

if GEOMETRY:
    import numpy as np
    from reportlab.pdfgen import canvas as pdfcanvas

    import assembly_extras
    import assembly_pdf
    import assembly_render as render
    import assembly_rules as rules
    from tests.test_assembly_guide import box_mesh


def make_scene(boxes: dict, mover: int = 2, rods_at=((-6.0, 5.0, 5.0), (-6.0, 8.0, 5.0)), direction="x"):
    """Parts from `boxes` {number: (x0, y0, z0, x1, y1, z1)}; rods join the mover to part 1, along `direction`."""
    result = {"parts": [part(n, b) for n, b in boxes.items()], "joints": [joint(mover, 1, 1)],
              "pins": [{"p": list(p), "axis": [1.0, 0, 0], "connector": "2 mm rod", "diameter": 2.0, "hole_diameter": 2.34,
                        "depth": 6.5, "parts": [mover, 1]} for p in rods_at]}
    return render.Scene.from_split(result, [box_mesh(boxes[n]) for n in sorted(boxes)])


def step_of(receiver, parts, direction):
    return {"id": "A1", "kind": "part", "parts": tuple(parts), "receiver": tuple(receiver), "direction": tuple(direction),
            "rods": 2, "connectors": {}, "glue_only": False, "section": None}


OPEN = {1: (0, 0, 0, 20, 10, 10), 2: (-10, 0, 0, 0, 10, 10)}
# part 3 is a tall wall beside the join on the +y side: it hides the face from the first-fit camera, a view from -y sees it
WALLED = {**OPEN, 3: (-30, 10, 0, 0, 12, 40)}
# a closed tunnel, open only at its -x end, with the join face at the far end: no diagonal view sees into it
TUNNEL = {**OPEN, 3: (-60, -2, 0, 0, 0, 10), 4: (-60, 10, 0, 0, 12, 10), 5: (-60, -2, 10, 0, 12, 12), 6: (-60, -2, -2, 0, 12, 0)}


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class QualityCameraTests(unittest.TestCase):
    def view(self, boxes, direction=(1.0, 0.0, 0.0), previous=None):
        scene = make_scene(boxes)
        step = step_of([n for n in boxes if n != 2], [2], direction)
        return scene, step, render.choose_view(scene, step, previous)

    def test_a_clear_join_keeps_the_first_fit_camera_with_no_close_up(self):
        scene, step, choice = self.view(OPEN)
        self.assertFalse(choice.scored)
        self.assertFalse(choice.close_up)
        np.testing.assert_allclose(choice.camera, render.choose_camera(None, step, None))

    def test_the_previous_camera_is_kept_while_it_shows_the_join(self):
        scene, step, first = self.view(OPEN)
        _, _, again = self.view(OPEN, previous=first.camera)
        np.testing.assert_allclose(again.camera, first.camera)

    def test_a_hidden_join_scores_ten_cameras_and_takes_one_that_sees_it(self):
        scene, step, choice = self.view(WALLED)
        first = render.choose_camera(None, step, None)
        self.assertGreater(first[1], 0, "the first-fit camera looks from the walled side")
        self.assertTrue(choice.scored)
        self.assertLess(choice.camera[1], 0, "the chosen view is from the open side")
        self.assertGreaterEqual(choice.quality["face"], 0.85)
        self.assertFalse(choice.close_up)

    def test_a_join_no_camera_shows_gets_a_close_up(self):
        _, _, choice = self.view(TUNNEL)
        self.assertTrue(choice.scored)
        self.assertTrue(choice.close_up)
        self.assertLess(choice.quality["score"], rules.CLOSE_UP_BELOW)

    def test_the_candidates_are_ten_and_the_choice_is_deterministic(self):
        self.assertEqual(len(render.scoring_candidates(np.array([1.0, 0, 0]))), 10)
        self.assertEqual(len(render.scoring_candidates(np.array([0.0, 0, -1.0]))), 10)
        for boxes in (OPEN, WALLED, TUNNEL):
            a, b = self.view(boxes)[2], self.view(boxes)[2]
            np.testing.assert_array_equal(a.camera, b.camera)
            self.assertEqual((a.scored, a.close_up), (b.scored, b.close_up))


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class TurnTests(unittest.TestCase):
    def test_only_upward_joins_turn(self):
        for direction, turns in (((0, 0, 1), True), ((0, 0, -1), False), ((1, 0, 0), False), ((0, -1, 0), False),
                                 ((0.2, 0, 1), True), ((1, 0, 0.3), False)):
            self.assertEqual(render.pose_for({"direction": direction}) is not None, turns, direction)

    def test_the_turn_makes_the_join_go_down_and_keeps_rods_with_their_parts(self):
        boxes = {1: (0, 0, 10, 10, 10, 20), 2: (0, 0, 0, 10, 10, 10)}
        scene = make_scene(boxes, rods_at=((5.0, 5.0, 10.0),))
        step = step_of([1], [2], (0, 0, 1))
        turned_scene, turned_step, turn = render.posed(scene, step)
        np.testing.assert_allclose(turned_step["direction"], (0, 0, -1), atol=1e-9)
        np.testing.assert_allclose(turn @ np.array([0.0, 0, 1]), [0, 0, -1], atol=1e-9)
        pin = turned_scene.pins[0]
        top = turned_scene.meshes[1].bounds
        self.assertAlmostEqual(pin["p"][2], turned_scene.meshes[2].bounds[0][2], delta=1e-6)   # the rod is still on the join face
        self.assertLess(top[1][2], turned_scene.meshes[2].bounds[1][2] + 1e-6)                   # the receiver is now under the mover
        self.assertEqual(scene.meshes[1].bounds[0][2], 10, "the shared scene is left in the finished pose")

    def test_an_upward_step_is_drawn_turned_from_above_and_a_level_one_is_not(self):
        boxes = {1: (0, 0, 10, 10, 10, 20), 2: (0, 0, 0, 10, 10, 10)}
        scene = make_scene(boxes, rods_at=((5.0, 5.0, 10.0),))
        up = step_of([1], [2], (0, 0, 1))
        camera = render.choose_camera(scene, up, None)
        self.assertGreater(camera[2], 0)
        art = render.prepare_step(scene, up, camera)
        self.assertIn("turn", art.extras)
        self.assertEqual(art.step, up)
        self.assertLess(art.direction[2], -0.99)
        flat = step_of([1], [2], (1, 0, 0))
        self.assertNotIn("turn", render.prepare_step(make_scene(OPEN), step_of([1], [2], (1, 0, 0)),
                                                     render.choose_camera(None, flat, None)).extras)

    def test_the_cue_and_close_up_draw_and_a_close_up_step_takes_a_page(self):
        scene = make_scene(TUNNEL)
        step = step_of([1, 3, 4, 5, 6], [2], (1, 0, 0))
        choice = render.choose_view(scene, step, None)
        plain = render.prepare_step(scene, step, choice.camera)
        art = render.prepare_step(scene, step, choice.camera, close_up=True)
        width = 190 * render.MM
        self.assertGreater(render.step_height(art, width), render.step_height(plain, width))
        buffer = io.BytesIO()
        c = pdfcanvas.Canvas(buffer)
        used = render.draw_step(c, art, 0, 800, width)
        self.assertAlmostEqual(used, render.step_height(art, width))
        flow = assembly_pdf.PageFlow(c, "a4")
        block, _lines = assembly_pdf._step_block(flow, art, step, {})
        self.assertGreater(block, flow.content.height / 2, "no second step fits on the page")
        c.showPage()
        c.save()
        # a turned step draws its cue
        boxes = {1: (0, 0, 10, 10, 10, 20), 2: (0, 0, 0, 10, 10, 10)}
        up = step_of([1], [2], (0, 0, 1))
        turned = render.prepare_step(make_scene(boxes, rods_at=((5.0, 5.0, 10.0),)), up, render.choose_camera(None, up, None))
        c = pdfcanvas.Canvas(io.BytesIO())
        render.draw_step(c, turned, 0, 800, width)
        c.save()


class RecordingCanvas(pdfcanvas.Canvas if GEOMETRY else object):
    """A canvas that remembers the greys, line weights and text it was asked for, and the lines it drew."""

    def __init__(self):
        super().__init__(io.BytesIO())
        self.greys, self.weights, self.texts, self.segments = [], [], [], []

    def setStrokeGray(self, gray, alpha=None):
        self.greys.append(round(gray, 4))
        super().setStrokeGray(gray, alpha)

    def setFillGray(self, gray, alpha=None):
        self.greys.append(round(gray, 4))
        super().setFillGray(gray, alpha)

    def setLineWidth(self, width):
        self.weights.append(round(width, 4))
        super().setLineWidth(width)

    def drawString(self, x, y, text, *args, **kwargs):
        self.texts.append(text)
        super().drawString(x, y, text, *args, **kwargs)

    def drawCentredString(self, x, y, text, *args, **kwargs):
        self.texts.append(text)
        super().drawCentredString(x, y, text, *args, **kwargs)

    def drawRightString(self, x, y, text, *args, **kwargs):
        self.texts.append(text)
        super().drawRightString(x, y, text, *args, **kwargs)

    def line(self, x1, y1, x2, y2):
        self.segments.append(float(np.hypot(x2 - x1, y2 - y1)))
        super().line(x1, y1, x2, y2)


# part 3 sits clear of the join (a placed part that does not touch the moving part 2); part 1 receives it
APART = {**OPEN, 3: (0, 30, 0, 20, 40, 10)}


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class DrawingRulesTests(unittest.TestCase):
    """The drawing rules (greys, weights, mark sizes, zoom, camera preference) live in assembly_rules: changing a rule there
    changes the drawing, and nothing else in the drawing code carries its own copy."""

    def art(self, boxes=OPEN, receiver=(1,), close_up=False):
        scene = make_scene(boxes)
        step = step_of(receiver, [2], (1.0, 0.0, 0.0))
        return scene, step, render.prepare_step(scene, step, render.choose_camera(scene, step, None), close_up=close_up)

    def drawn(self, draw, art, frame=(0.0, 0.0, 300.0, 200.0)):
        canvas = RecordingCanvas()
        draw(canvas, art, frame)
        return canvas

    def test_frame_one_uses_the_receiver_rule(self):
        _, _, art = self.art()
        with mock.patch.object(rules, "RECEIVER", (0.123, 0.777)):
            canvas = self.drawn(render.draw_frame_one, art)
        self.assertIn(0.123, canvas.greys)
        self.assertIn(0.777, canvas.weights)

    def test_frame_two_uses_the_touching_placed_and_bold_rules(self):
        _, _, art = self.art(APART, receiver=(1, 3))
        with mock.patch.object(rules, "TOUCHING", (0.211, 0.371)), mock.patch.object(rules, "PLACED", (0.733, 0.137)), \
                mock.patch.object(rules, "BOLD", 1.91), mock.patch.object(rules, "HALO", 2.77):
            canvas = self.drawn(render.draw_frame_two, art)
        for grey in (0.211, 0.733):
            self.assertIn(grey, canvas.greys)
        for weight in (0.371, 0.137, 1.91, round(1.91 + 2.77, 4)):
            self.assertIn(weight, canvas.weights)

    def test_the_rod_length_floor_is_a_rule(self):
        _, _, art = self.art()
        before = max(self.drawn(render.draw_frame_one, art).segments)
        with mock.patch.object(rules, "MIN_ROD_PT", 200.0):
            after = max(self.drawn(render.draw_frame_one, art).segments)
        self.assertGreater(after, 2 * before)

    def test_the_bit_size_threshold_is_a_rule(self):
        scene, step, art = self.art()
        self.assertTrue(art.runs_one[("r", 1)])
        with mock.patch.object(rules, "MIN_BIT_PX", 1e9):
            bare = render.prepare_step(scene, step, render.choose_camera(scene, step, None))
        self.assertFalse(bare.runs_one[("r", 1)], "every disconnected bit is under the threshold, so no line survives")

    def test_the_close_up_zoom_is_a_rule(self):
        with mock.patch.object(rules, "CLOSE_UP_ZOOM", 5.0):
            _, _, art = self.art(close_up=True)
            canvas = RecordingCanvas()
            render.draw_close_up(canvas, art, 0.0, 800.0, 400.0)
        self.assertEqual(art.extras["close_up"].zoom, 5.0)
        self.assertIn("Close-up, about 5x", canvas.texts)

    def test_the_zoom_circle_size_is_a_rule(self):
        _, _, art = self.art()
        frame = (0.0, 0.0, 400.0, 300.0)
        with mock.patch.object(rules, "ZOOM_MM", 40.0):
            found = assembly_extras.zoom_circle(art, frame)
        self.assertIsNotNone(found)
        self.assertAlmostEqual(found[1], 20.0 * render.MM)

    def test_the_thumbnail_greys_are_rules(self):
        scene, _, art = self.art(APART, receiver=(1, 3))
        assembly_extras.attach(art, scene, [1])           # part 3 is outside the Section, so it takes the light grey
        with mock.patch.object(rules, "THUMB_SECTION", (0.151, 0.41)), mock.patch.object(rules, "THUMB_REST", (0.919, 0.29)):
            canvas = RecordingCanvas()
            assembly_extras.draw_thumbnail(canvas, art, 0.0, 200.0, 140.0, 100.0)
        for grey in (0.151, 0.919):
            self.assertIn(grey, canvas.greys)
        for weight in (0.41, 0.29):
            self.assertIn(weight, canvas.weights)

    def test_the_camera_elevation_is_a_rule(self):
        base = render.camera_candidates()[0]
        with mock.patch.object(rules, "CAMERA_ELEVATION", 2.0):
            steep = render.camera_candidates()[0]
        self.assertGreater(steep[2], base[2])
        np.testing.assert_allclose(steep, render.unit([1, 1, 2.0]))

    def test_the_first_fit_angle_is_a_rule(self):
        # of two views that both see the face, the one whose angle to the insertion axis is nearest the preferred angle wins
        step = {"direction": (0.6, 0.8, 0.0)}
        base = render.choose_camera(None, step, None)
        with mock.patch.object(rules, "FIRST_FIT_DEGREES", 85.0):
            flatter = render.choose_camera(None, step, None)
        self.assertLess(base[0], 0)
        self.assertGreater(flatter[0], 0)

    def test_the_rules_are_the_only_home_of_the_values(self):
        for name in ("RECEIVER", "TOUCHING", "PLACED", "BOLD", "HALO", "CLOSE_UP_BELOW", "MIN_ARROW_PT", "THUMB_SECTION", "THUMB_REST",
                     "ZOOM_MM", "CAMERA_ELEVATION", "FIRST_FIT_DEGREES", "MIN_ROD_PT", "MIN_BIT_PX", "PICTURE_MIN_BIT_PX"):
            self.assertTrue(hasattr(rules, name), name)
            self.assertFalse(hasattr(render, name) and getattr(render, name) is not getattr(rules, name), name)


# a tall, thin stack (60 mm tall on a 10 mm footprint) that part 2 joins from the -x side
TALL = {1: (0, 0, 0, 10, 10, 60), 2: (-10, 0, 0, 0, 10, 10)}
EDGE_ON = render.unit([-1.0, 0.1, 0.8]) if GEOMETRY else None      # looks straight at the -x side of the stack: one side only


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class TallStackCameraTests(unittest.TestCase):
    def choose(self, boxes, previous):
        scene = make_scene(boxes)
        step = step_of([n for n in boxes if n != 2], [2], (1.0, 0.0, 0.0))
        return render.choose_view(scene, step, previous)

    def sides_seen(self, camera, bounds=(10, 10)):
        # the stack's side faces face +-x and +-y: a side is seen when the camera is clearly in front of it
        return sum(1 for axis in (0, 1) for sign in (1, -1) if sign * camera[axis] > rules.TALL_SIDE_FACING)

    def test_a_tall_stack_seen_edge_on_gets_a_three_quarter_camera_showing_two_sides(self):
        self.assertEqual(self.sides_seen(EDGE_ON), 1)
        choice = self.choose(TALL, EDGE_ON)
        self.assertGreaterEqual(self.sides_seen(choice.camera), 2)
        self.assertGreater(choice.camera[2], 0, "from above")
        self.assertGreater(choice.camera @ np.array([-1.0, 0, 0]), rules.SEES_FACING, "the join face is still seen")

    def test_a_stack_that_is_not_tall_keeps_the_reused_camera(self):
        choice = self.choose(OPEN, EDGE_ON)
        np.testing.assert_allclose(choice.camera, EDGE_ON)

    def test_a_tall_stack_already_shown_from_two_sides_keeps_its_camera(self):
        diagonal = render.unit([-1.0, 1.0, 0.8])
        np.testing.assert_allclose(self.choose(TALL, diagonal).camera, diagonal)

    def test_the_tall_ratio_is_a_rule_and_the_choice_is_deterministic(self):
        with mock.patch.object(rules, "TALL_STACK_RATIO", 50.0):
            np.testing.assert_allclose(self.choose(TALL, EDGE_ON).camera, EDGE_ON)
        a, b = self.choose(TALL, EDGE_ON).camera, self.choose(TALL, EDGE_ON).camera
        np.testing.assert_array_equal(a, b)


# two stacked moving parts (2 and 4) against placed parts (1 and 3) and a wall (5): the number bubbles crowd each other
STACKED = {1: (0, 0, 0, 20, 10, 10), 2: (-10, 0, 0, 0, 10, 10), 3: (0, 0, 10, 20, 10, 20), 4: (-10, 0, 10, 0, 10, 20),
           5: (-30, 0, 0, -12, 10, 40)}    # part 5 stands left of the moving parts, where their badges would first go


def _ink_samples(art, box, step_pt=1.5):
    """Points along every line the second frame draws, in page points."""
    out = []
    for runs in art.runs_two.values():
        for a, b in runs:
            p, q = box.px(np.array([a, b]))
            n = max(2, int(np.hypot(*(q - p)) / step_pt))
            out.extend(p + (q - p) * t for t in np.linspace(0, 1, n))
    return np.array(out)


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class BadgeAndTurnCueTests(unittest.TestCase):
    FRAME = (0.0, 0.0, 300.0, 200.0)

    def stacked_art(self):
        scene = make_scene(STACKED)
        step = step_of([1, 3, 5], [2, 4], (1.0, 0.0, 0.0))
        return render.prepare_step(scene, step, render.choose_camera(scene, step, None))

    def test_no_badge_overlaps_another_badge_or_hides_behind_a_part(self):
        art = self.stacked_art()
        badges = render.badge_layout(art, self.FRAME)
        self.assertEqual(sorted(b.text for b in badges), ["1", "2", "4"])
        for i, a in enumerate(badges):
            for b in badges[i + 1:]:
                self.assertGreaterEqual(float(np.hypot(*(a.centre - b.centre))), a.radius + b.radius, (a.text, b.text))
        ink = _ink_samples(art, render.Box(self.FRAME, art.view))
        for badge in badges:
            gap = np.min(np.hypot(*(ink - badge.centre).T))
            self.assertGreaterEqual(gap, badge.radius, f"badge {badge.text} sits on a part")

    def test_badges_keep_inside_the_frame_and_clear_of_the_frame_number(self):
        art = self.stacked_art()
        x, y, w, h = self.FRAME
        corner = np.array([x + 9, y + h - 9])
        for badge in render.badge_layout(art, self.FRAME):
            self.assertTrue(x <= badge.centre[0] - badge.radius and badge.centre[0] + badge.radius <= x + w)
            self.assertTrue(y <= badge.centre[1] - badge.radius and badge.centre[1] + badge.radius <= y + h)
            self.assertGreaterEqual(float(np.hypot(*(badge.centre - corner))), badge.radius + rules.BADGE_RADIUS)

    def test_a_badge_that_had_to_move_is_marked_and_the_layout_is_deterministic(self):
        art = self.stacked_art()
        first = render.badge_layout(art, self.FRAME)
        again = render.badge_layout(art, self.FRAME)
        self.assertEqual([(b.text, tuple(b.centre), b.moved) for b in first], [(b.text, tuple(b.centre), b.moved) for b in again])
        self.assertTrue(any(b.moved for b in first), "the crowded stack forces at least one badge off its first corner")
        for badge in first:
            self.assertEqual(len(badge.anchor), 2, "every badge has a leader to the part it names")

    def test_the_turn_cue_sits_between_the_frames_and_shows_the_flip_axis(self):
        boxes = {1: (0, 0, 10, 10, 10, 20), 2: (0, 0, 0, 10, 10, 10)}
        up = step_of([1], [2], (0, 0, 1))
        scene = make_scene(boxes, rods_at=((5.0, 5.0, 10.0),))
        art = render.prepare_step(scene, up, render.choose_camera(scene, up, None))
        width = 190 * render.MM
        first, second = render.layout_frames(art, width)
        cue = render.turn_cue_layout(art, 100.0, 800.0, width)
        gap_middle = 100.0 + first[2] + render.FRAME_GAP / 2
        self.assertAlmostEqual(cue.centre[0], gap_middle, delta=0.5)
        self.assertLess(cue.centre[0] - cue.radius, 100.0 + second[0], "it reaches into the first frame's side")
        self.assertGreater(cue.centre[0] + cue.radius, 100.0 + second[0], "and into the second's")
        self.assertAlmostEqual(float(np.hypot(*cue.axis)), 1.0, places=6)
        # the model is turned half a turn about a horizontal axis: world x or y, never up
        np.testing.assert_allclose(sorted(np.abs(art.extras["turn_axis"])), [0, 0, 1], atol=1e-9)
        self.assertAlmostEqual(art.extras["turn_axis"][2], 0.0, places=9)

    def test_the_cue_is_drawn_on_exactly_the_turned_steps(self):
        width = 190 * render.MM
        turned_boxes = {1: (0, 0, 10, 10, 10, 20), 2: (0, 0, 0, 10, 10, 10)}
        up = step_of([1], [2], (0, 0, 1))
        flat = step_of([1], [2], (1.0, 0, 0))
        for step, boxes, rods, turned in ((up, turned_boxes, ((5.0, 5.0, 10.0),), True), (flat, OPEN, ((-6.0, 5.0, 5.0),), False)):
            scene = make_scene(boxes, rods_at=rods)
            art = render.prepare_step(scene, step, render.choose_camera(scene, step, None))
            canvas = RecordingCanvas()
            render.draw_step(canvas, art, 0.0, 800.0, width)
            self.assertEqual("Turn it over" in canvas.texts, turned)
            self.assertEqual(render.turn_cue_layout(art, 0.0, 800.0, width) is not None, turned)
