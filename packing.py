"""
How far apart objects sit on a build plate, and from its edge: the constants the nester (`nesting`) and the packing density
presets (`project_read`) share. Layouts themselves are made by `nesting.nest`, which the planner's greedy first-fit seed and
search use.
"""

GAP_MM = 6.0
SUPPORT_GAP_MM = 22.0  # tree supports branch out past an object's footprint (measured reach 21 mm plus margin); closer objects make OrcaSlicer's toolpaths collide
MARGIN_MM = 3.0
