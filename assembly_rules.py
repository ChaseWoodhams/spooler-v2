"""The drawing rules of the Assembly guide, in one place.

Every grey, line weight, mark-size threshold, zoom value and camera preference the guide's drawings use is a named value
here. The drawing code (assembly_render, assembly_extras, assembly_inventory) reads them as `rules.NAME` when it draws, so a
later adjustment is one change in this file, and a test can set a rule to see that the drawing follows it.

A grey is 0 (black) to 1 (white). A "(grey, weight)" pair is a stroke: the grey and the line weight in points. A weight or
length in "pt" is PDF points, "px" is a pixel of the depth buffer a frame is rendered into, "mm" is model millimetres.
This module holds numbers only: no imports, nothing computed.
"""

# ── Line extraction: how a mesh becomes lines ──────────────────────────────────
DECIMATE_MM = 0.25            # part meshes are simplified to this tolerance before drawing
CREASE_DEG = 35.0             # an edge between faces more than this far from flat is drawn as a crease
BIAS = 0.0035                 # depth-test bias as a share of the view diagonal
BRIDGE_PX = 12                # silhouette gaps up to this are bridged
MIN_BIT_PX = 30               # disconnected bits shorter than this are dropped
PICTURE_MIN_BIT_PX = 12       # the same for the small pictures (Section thumbnails, inventory cards, "Where this is")
RENDER_PX = 1200              # width of a frame's depth buffer (kept small enough for a quick run)
SAMPLE_PX = 0.5               # spacing of the visibility samples along an edge
FACE_TOLERANCE_MM = 0.5       # vertices this close to the join plane belong to the join face
STRAY_MARK_MM = 6.0           # a small surface mark whose lines total less than this is suppressed ...
STRAY_NEIGHBOUR_MM = 8.0      # ... unless another small mark is this close (the digits and underline of a part number) ...
                              # ... or it is a hole (it lies by a rod's axis)
JOIN_REACH_MM = 0.3           # a face edge within this of the other side's face is part of the join edge

# ── Greys and weights of the step frames ───────────────────────────────────────
RECEIVER = (0.25, 0.7)        # the receiving unit, frame 1
TOUCHING = (0.4, 0.35)        # placed parts touching the moving unit, frame 2
PLACED = (0.55, 0.25)         # every other placed part, frame 2
GHOST_FLOOR = 0.55            # no placed part is drawn lighter than this grey, so it survives a laser printer or a photocopy
GHOST_HAIRLINE_PT = 0.4       # a placed part's outline is a hairline: never heavier than this
JOIN_EDGE_STEP = 0.3          # the join edge is this much heavier than the surface creases of the same part (kept under BOLD)
DEPTH_GREY = 0.62             # the depth cue on a curved shell part: light hatching taken from the mesh normals ...
DEPTH_GREY_FLOOR = 0.55       # ... never a grey darker than this
DEPTH_WEIGHT_PT = 0.35
DEPTH_SPACING_PT = 4.5        # distance between hatch lines on the page (widened when the limit below needs it)
DEPTH_MAX_DARKNESS = 0.08     # share of a part's area the cue may blacken: (1 - grey) x weight / spacing, and the same in ink
DEPTH_REF_FRAME_PT = 240.0    # a frame is about this wide on Letter paper: where points per pixel is taken for the spacing
DEPTH_GRAZE = 0.5             # surfaces turned this far from facing the camera (dot of normal and view below it) are hatched
DEPTH_ANGLE_DEG = 45.0        # hatching on the outside runs at this angle on the page, the inside at the opposite angle
DEPTH_MIN_BIT_PX = 8          # hatch lines are short: shorter pieces than this are dropped (the creases keep MIN_BIT_PX)
CURVE_MIN_DEG = 2.0           # a mesh edge bending between this and CREASE_DEG is a smooth curve ...
CURVED_SHARE = 0.25           # ... and a part with this share of such edges among its bent ones is curved (a shell), else flat
BOLD = 1.2                  # the moving unit: black with a white halo
HALO = 1.8
PICTURE = (0.3, 0.45)         # a small picture of finished parts
THUMB_SECTION = (0.35, 0.5)   # "Where this is": the step's Section
THUMB_REST = (0.82, 0.3)      # "Where this is": everything else
THUMB_HALO = 0.8              # extra white halo of the step's own parts in "Where this is"
FRAME_BORDER = (0.82, 0.5)    # the box round a frame
PATH_LINE = (0.35, 0.6)       # the dashed path of the moving unit
PATH_DASH = (3, 3)
LEADER_WEIGHT = 0.6           # leader lines (bubbles, the glue drop)
CARD_BORDER = (0.6, 0.5)      # the box round an inventory card

# ── Marks: rods, glue, arrows, badges ──────────────────────────────────────────
MIN_ROD_PT = 15.0             # rods stand out of the face at least this long, so they stay visible
ROD_MIN_WIDTH_PT = 3.0        # a rod is never drawn thinner than this
ROD_OUTLINE_PT = 1.4          # the black outline round a rod's white core
ROD_CORE_TRIM_PT = 0.6        # the white core is this much thinner than the rod
ROD_CORE_MIN_PT = 0.8
ROD_TIP_MM = 0.4              # a rod's far end stands this far past the face
MIN_ARROW_PT = 46.0
ARROW_WEIGHT = 2.6
ARROW_HEAD_PT = 11
STIPPLE_SPACING_PT = 3.4      # the glue stipple on the join face
STIPPLE_GREY = 0.2
STIPPLE_DOT_PT = 0.45
STIPPLE_MAX_DOTS = 40000      # a face needing more dots than this is left unstippled
GLUE_DROP_PT = 8
GLUE_DROP_GAP_PT = 26         # the glue drop sits this far from the rods
BUBBLE_BOLD = (8.5, 1.0)      # radius and ring weight of a part number bubble, moving part
BUBBLE_LIGHT = (8.0, 0.7)     # the same for a placed part
BUBBLE_LIGHT_TEXT = 0.4
BADGE_RADIUS = 7.5            # the frame number in the corner of a frame
CUE_MM = 12.0                 # the "turn it over" cue
CUE_WEIGHT = 1.6

# ── Zoom, close-up and the camera ──────────────────────────────────────────────
CLOSE_UP_ZOOM = 3.0           # the close-up frame shows the join this many times larger than frame 1
CLOSE_UP_PX = 800
CLOSE_UP_BELOW = 0.70         # when even the best candidate scores under this, the step gets a close-up frame
CLEAR_SHARE = 0.85            # a camera showing this much of the join face and of the rod tips is kept
QUALITY_PX = 500              # width of the low-resolution depth buffer the candidates are scored on
FACE_SAMPLES = 300
ZOOM_MM = 26.0                # the zoom circle is about this wide
ZOOM_CLEAR_PT = 3.0           # points kept between the circle and the nearest rod
ZOOM_LABEL_PT = 12.0          # room under the circle for its "Zoom 3x" label
ZOOM_FILL = 0.8               # share of the circle the rods may spread over
ZOOM_RANGE = (2.0, 8.0)       # the magnification is kept between these
ZOOM_MIN_SPREAD_PT = 6.0

CAMERA_ELEVATION = 0.8        # the diagonal views look down (or up) at this height against a unit sideways
FIRST_FIT_DEGREES = 55        # first fit prefers the view nearest this angle to the insertion axis
REUSE_FACING = 0.3            # the previous camera is kept while its dot with the face normal is above this
SEES_FACING = 0.05            # a camera sees the face when its dot with the face normal is above this
SCORE_WEIGHTS = (0.5, 0.3, 0.2)   # share of the join face seen, of the rod tips seen, how squarely it faces the join
SCORE_FACING_FULL = 0.5       # facing at or above this counts in full
SCORE_ABOVE_BONUS = 0.03      # a view from above is preferred on a tie
THUMB_CAMERA = 0              # "Where this is" and the Section pictures use the first diagonal view
ZOOM_LEADER = (0.45, 0.5)     # the thin leader from the zoom circle to the rods it magnifies
ZOOM_RING_PT = 0.9            # the ring round the zoom circle
ZOOM_LINE_SCALE = 1.4         # lines inside the circle are this much heavier than in the frame, before magnifying

# ── Inventory cards ────────────────────────────────────────────────────────────
THIN_WIDTH_MM = 12.0          # a part whose middle dimension is under this gets a second view at another angle
TALL_ASPECT = 3.0             # a part this many times longer than wide, or any thin part, gets a taller cell
TALL_FACTOR = 1.6             # a tall cell is this many times an ordinary row height
STRIP_MM = 14.0               # height of the strip of all parts at one common scale
