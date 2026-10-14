"""Camera quality for the Assembly guide drawings: reuse, the scored fallback, turning a model over for an upward join, and
the close-up frame.

The seam is assembly_render's camera choice (choose_view / choose_camera) and the step art prepare_step makes from it. Scenes
are small boxes written straight into a Scene, as the guide would see a split. Tests assert on external facts (which steps
turn, which steps get a close-up, which side the camera is on, determinism), never on pixels."""
import io
import unittest

from tests.test_assembly_plan import GEOMETRY, part, joint

if GEOMETRY:
    import numpy as np
    from reportlab.pdfgen import canvas as pdfcanvas

    import assembly_pdf
    import assembly_render as render
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
        self.assertLess(choice.quality["score"], render.CLOSE_UP_BELOW)

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
        block, _lines = assembly_pdf._step_block(flow, art, step)
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
